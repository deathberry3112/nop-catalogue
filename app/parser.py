"""Parse the uploaded workbook into rows — the INPUT side.

    app -> parser.py -> rows -> runner -> summary -> xlsxCreator.py -> out.xlsx

The inverse of xlsxCreator, and deliberately its mirror: the same 12 template
columns, read instead of written. A template change is an edit in two files and
the round-trip test (tests/test_parser.py) fails until both are done.

    result = parse_workbook(path)
    result.rows      -> the same shape main.load_rows() produces
    result.errors    -> problems that stopped a sheet being read at all

ONE SHEET ROW IS ONE VARIANT, NOT ONE PRODUCT. This is the template change the
rest of the module is shaped around. The sheet spells out every combination
individually — one colour, one size, one child SKU per line — and rows sharing
a SKU are one product:

    Product name  SKU          Color   Size     Child SKU Varient
    G55FF - M     G55FF - M    Lime    S/M      G55FF08
    G55FF - M     G55FF - M    Lime    L/XL     G55FF10
    G55FF - M     G55FF - M    Orange  S/M      G55FF14
    G55FF - M     G55FF - M    Orange  L/XL     G55FF16

    -> one product, 2 colours x 2 sizes, and the sheet SAYS which child SKU
       belongs to ("Orange", "L/XL") instead of leaving it to be inferred.

THE GROUPING KEY IS THE SKU, never the product name. Two rows may share a
product name and still be different products — the client's sheets do exactly
that, listing eight one-variant products under a single display name — so
grouping on the name would silently fuse eight products into one. The SKU is
what this automation looks a product up by and creates it with, so it is what
decides identity here.

WHAT THE OLD TEMPLATE COST, AND WHAT THIS BUYS. The previous layout packed a
product onto one line, with colours comma-separated and every child SKU in a
single cell, and never said which was which. combination_skus.py exists to
recover that pairing from the SKU text ("CP-30-C100V YW" -> Yellow) and
app/colour/ exists to fall back to a curated map and then a model when the text
does not give it up. None of that inference runs on this template: the pairing
is read, not guessed. Those modules stay in the tree — untouched and still
tested — because they remain the only thing that can read a workbook written
the old way, but nothing on this path calls them to *derive* anything any more.

That also makes the old layout unreadable HERE, and deliberately so. A cell
holding "Lime, Orange" against nine child SKUs is not a variant line; parsing
it as one would produce a product with two colours and nine unpaired children
and no complaint. So a sheet still written that way is refused with a message
saying which it is — see _detect_legacy_layout.

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

#: The per-variant fields. Everything else on a line describes the PRODUCT, and
#: so has to agree across every line sharing a SKU — see _merge.
VARIANT_FIELDS = ("colors", "sizes", "combinations")

#: Product-level fields, and how to name them when two lines disagree. The
#: wording is what the user sees, so it names the column, not the field.
SHARED_FIELDS: dict[str, str] = {
    "product_name": "Product name",
    "spec_value": "Attribute Specification Value",
    "color_attr": "the first 'Product Attributes Type'",
    "size_attr": "the second 'Product Attributes Type'",
    "generate_combinations": "Generate Combinations",
    "allow_out_of_stock": "Allow Out Of Stock",
    "save_product": "Save Product",
}


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
    newlines (the combination column) and commas (colours, sizes).

    On this template every one of those cells should hold exactly ONE entry, so
    this is now used to detect the old layout rather than to read it — a cell
    that splits into two is a line describing a whole product. Sizes are the
    reason it still splits on commas rather than being read verbatim: "S/M" and
    "4XL/5XL" are single sizes that happen to contain a slash, so the separator
    has to stay the comma to leave them intact.
    """
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


def _read_variant(values: tuple, mapping: dict[int, str], *, sheet: str,
                  row_number: int) -> dict | None:
    """One worksheet line -> one variant record, or None if the line is blank.

    Nothing is validated here. This is a transcription: the cells this line
    holds, split out of the sheet and into fields, so _detect_legacy_layout can
    judge the sheet's shape and _merge can group by SKU. Validation happens on
    the assembled product, because that is the level most of the questions are
    asked at ("do these colours and sizes cover every child SKU?").
    """
    raw = {field: values[i] for i, field in mapping.items() if i < len(values)}
    if not any(_text(v) for v in raw.values()):
        return None

    return {
        "sku": _text(raw.get("sku")),
        "product_name": _text(raw.get("product_name")) or _text(raw.get("sku")),
        "spec_value": _text(raw.get("spec_value")),
        "color_attr": _text(raw.get("color_attr")) or "Color",
        "size_attr": _text(raw.get("size_attr")) or "Size",
        "colors": _split(raw.get("colors")),
        "swatches": _split(raw.get("swatches")),
        "sizes": _split(raw.get("sizes")),
        "combinations": _split(raw.get("combinations")),
        "generate_combinations": _flag(raw.get("generate_combinations")),
        "allow_out_of_stock": _flag(raw.get("allow_out_of_stock")),
        "save_product": _flag(raw.get("save_product"), default=True),
        "status": _text(raw.get("status")),
        "source_sheet": sheet,
        "source_row": row_number,
        # Whether the sheet named the size attribute at all, per line, so the
        # assembled product can warn about it without re-reading the mapping.
        "_has_size_column": "size_attr" in mapping.values(),
    }


def _detect_legacy_layout(sheet: str, variants: list[dict]) -> str | None:
    """Is this sheet written to the OLD one-row-per-product template?

    Told apart by arity, which is the only difference that cannot be faked: on
    this template a line carries one colour, one size and one child SKU, so any
    cell holding two entries is a line describing a whole product.

    Refusing beats reading it. The two layouts disagree about what a row MEANS,
    not about how it is spelled, and the columns are identical — so a legacy
    sheet parsed as variants comes out as a product per line, each with a
    colour list where a colour should be and a pile of unpaired child SKUs.
    Every check below would pass it. It would reach the runner and create a
    catalogue nobody asked for, which is precisely the silent-wrong-catalogue
    outcome this module is written to prevent.

    A legacy sheet holding exactly one product with one colour, one size and
    one child SKU is indistinguishable from a variant sheet — and needs no
    telling apart, because the two readings produce the same product.
    """
    for variant in variants:
        packed = {label: len(variant[field])
                  for field, label in (("colors", "colour"), ("sizes", "size"),
                                       ("combinations", "child SKU"))
                  if len(variant[field]) > 1}
        if not packed:
            continue
        listed = ", ".join(f"{count} {label}s" for label, count in packed.items())
        return (
            f"{sheet}: this sheet is written to the OLD template — row "
            f"{variant['source_row']} ({variant['sku'] or 'no SKU'}) packs "
            f"{listed} onto one line. The current template is one line per "
            f"variant: one colour, one size and one child SKU each, with every "
            f"line of a product repeating its SKU. Re-export the sheet in that "
            f"format. Nothing on this sheet was read.")
    return None


def _merge(variants: list[dict], unknown_headers: list[str]) -> dict:
    """A product's variant lines -> the one row the rest of the pipeline runs.

    The assembled row is the shape it has always been — colours, sizes and
    child SKUs as parallel lists — so the runner, the report and the rollback
    need no changes. What is new is `variants`, which carries the pairing the
    sheet stated:

        row["variants"] -> [{"color": "Lime", "size": "S/M", "sku": "G55FF08"},
                            ...]

    combination_skus.combination_map() reads that instead of inferring it. It
    is a list of dicts rather than the {(colour, size): sku} mapping the runner
    wants because rows are JSON — they are persisted to the run store and
    handed to the browser — and a tuple key survives neither.
    """
    first = variants[0]

    row: dict[str, Any] = {
        "sku": first["sku"],
        "product_name": first["product_name"],
        "spec_value": first["spec_value"],
        "color_attr": first["color_attr"],
        "size_attr": first["size_attr"],
        "colors": _unique(v["colors"][0] for v in variants if v["colors"]),
        "sizes": _unique(v["sizes"][0] for v in variants if v["sizes"]),
        "swatches": [],
        "combinations": [v["combinations"][0] for v in variants if v["combinations"]],
        "variants": [],
        "generate_combinations": first["generate_combinations"],
        "allow_out_of_stock": first["allow_out_of_stock"],
        "duplicates": {},          # legacy-layout affordance; unreachable here
        "save_product": first["save_product"],
        "source_sheet": first["source_sheet"],
        "source_row": first["source_row"],
        "source_rows": [v["source_row"] for v in variants],
        "errors": [],
        "warnings": [],
    }

    # One swatch per colour, taken from the lines naming that colour. Under the
    # old template the two columns were parallel lists and stayed aligned only
    # by luck; here each line carries its own pair, so the swatch cannot drift
    # onto the wrong colour. A colour whose lines disagree is reported by
    # _validate rather than resolved.
    swatches: dict[str, list[str]] = {}
    for variant in variants:
        if variant["colors"] and variant["swatches"]:
            swatches.setdefault(variant["colors"][0], []).append(variant["swatches"][0])
    row["_swatches_by_colour"] = swatches
    row["swatches"] = [swatches[c][0] for c in row["colors"] if c in swatches]

    for variant in variants:
        if not variant["combinations"]:
            continue
        row["variants"].append({
            "color": variant["colors"][0] if variant["colors"] else "",
            "size": variant["sizes"][0] if variant["sizes"] else None,
            "sku": variant["combinations"][0],
            "source_row": variant["source_row"],
        })

    status = next((v["status"] for v in variants if v["status"]), "")
    if status:
        row["status"] = status                 # a re-uploaded report: resume it

    # The two "Product Attributes Type" columns are positional, so the second
    # one naming "Color" names the SAME attribute twice. Not a harmless typo:
    # add_attribute is idempotent BY NAME, so the size pass would fetch the
    # colour attribute back and file "S/M" as a ninth colour. A blank cell
    # already falls back to "Size"; a cell repeating the colour attribute means
    # the same thing, so it is treated the same way.
    #
    # The row warning stays short because the full explanation belongs to the
    # SHEET — see _check_attribute_columns. Repeating three sentences on every
    # one of twenty rows is noise, but the row still needs a warning so it does
    # not tick itself for committing.
    if row["sizes"]:
        if not first["_has_size_column"]:
            row["warnings"].append(SIZE_COLUMN_MISSING)
            row["size_attr"] = "Size"
        elif row["size_attr"] == row["color_attr"]:
            row["warnings"].append(SIZE_COLUMN_WRONG)
            row["size_attr"] = "Size"

    _validate(row, variants, unknown_headers)
    return row


def _validate(row: dict, variants: list[dict], unknown_headers: list[str]) -> None:
    """Everything wrong with this product, all at once."""
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

    # These columns describe the PRODUCT, so every line of it has to agree. Two
    # lines sharing a SKU and disagreeing about the name (or about whether to
    # save it) do not describe a product this can create — one of the two
    # answers would have to be discarded, and nothing here knows which.
    _check_agreement(row, variants)

    # Each colour's swatch comes off its own line now, so a disagreement is a
    # real contradiction rather than the old column-alignment guess.
    for colour, values in (row.pop("_swatches_by_colour", None) or {}).items():
        distinct = _unique(values)
        if len(distinct) > 1:
            errors.append(
                f"{colour!r} is given {len(distinct)} different swatches "
                f"{distinct} — one colour has one swatch")

    if row["swatches"] and len(row["swatches"]) != len(row["colors"]):
        warnings.append(
            f"{len(row['swatches'])} swatch(es) for {len(row['colors'])} colour(s)")

    _check_grid(row)

    # WHAT IS NOT CHECKED HERE, AND WHY IT USED TO BE. The old parser warned
    # when a child SKU did not start with the parent's, because the old
    # template made that prefix load-bearing: combination_skus.base_prefix
    # stripped it off to recover the colour code, so a parent SKU that did not
    # match its children was the first sign a row was about to be mapped wrong.
    #
    # This template states the pairing, so the prefix carries nothing. And the
    # client's own convention no longer follows it — parent "CP-280-HP241R - M"
    # against children "CP-280-HP241R-01" — so the check fired on every product
    # in a clean workbook. A warning that is always on is one users learn to
    # scroll past, taking the real ones with it.


def _check_agreement(row: dict, variants: list[dict]) -> None:
    """Product-level columns that two lines of one product disagree about."""
    for field, label in SHARED_FIELDS.items():
        # size_attr is only meaningful when the product has sizes, and _merge
        # may already have overridden it; judge what the sheet said, not that.
        if field == "size_attr" and not row["sizes"]:
            continue
        seen: dict[str, list[int]] = {}
        for variant in variants:
            value = variant[field]
            text = {True: "Yes", False: "No"}.get(value, value)
            seen.setdefault(str(text), []).append(variant["source_row"])
        if len(seen) > 1:
            shown = "; ".join(f"{value!r} on row(s) {rows}"
                              for value, rows in seen.items())
            row["errors"].append(
                f"the lines for {row['sku'] or 'this SKU'} disagree about "
                f"{label}: {shown}. Every line of a product must repeat the "
                f"same values in that column.")


def _check_grid(row: dict) -> None:
    """Do the variant lines cover every colour x size exactly once?

    The old template could only compare COUNTS — it had a colour list, a size
    list and a pile of child SKUs, and no statement of which belonged to which,
    so "8 children for 2 colours x 4 sizes" was the most it could check. This
    one names the pairs, so the check is the real one: every combination the
    product will generate has exactly one child SKU waiting for it.

    That matters because the runner generates the grid in nopCommerce and then
    looks each combination up. A pair with no child SKU fails mid-run with the
    product already created; a pair with two is a contradiction the sheet has
    to settle. Both are worth catching before the browser opens.
    """
    errors = row["errors"]

    if not row["variants"]:
        if row["generate_combinations"]:
            errors.append("Generate Combinations is set but no child SKUs are listed")
        return

    if not row["colors"]:
        errors.append("no colours are listed")
        return

    missing_colour = [v["source_row"] for v in row["variants"] if not v["color"]]
    if missing_colour:
        errors.append(f"row(s) {missing_colour} give a child SKU with no colour")

    # A product either has sizes on every line or on none. Half and half leaves
    # the sized lines describing a two-dimensional grid and the rest a
    # one-dimensional one, and no reading covers both.
    sized = [v for v in row["variants"] if v["size"]]
    if sized and len(sized) != len(row["variants"]):
        bare = [v["source_row"] for v in row["variants"] if not v["size"]]
        errors.append(
            f"row(s) {bare} leave the Size cell empty while the rest of this "
            f"product sets one — a product has sizes on every line or on none")
        return

    seen: dict[tuple[str, str | None], list[str]] = {}
    for variant in row["variants"]:
        seen.setdefault((variant["color"], variant["size"]), []).append(variant["sku"])

    for (colour, size), skus in seen.items():
        if len(skus) > 1:
            where = f"{colour} / {size}" if size else colour
            errors.append(
                f"{len(skus)} child SKUs for {where}: {skus}. One combination "
                f"takes one child SKU.")

    # Only when combinations are actually being generated. A product that is
    # not generating a grid has no grid to fill, and blocking it over a gap in
    # one it will never build would refuse a row the runner handles fine.
    expected = [(c, s) for c in row["colors"] for s in (row["sizes"] or [None])]
    absent = [pair for pair in expected if pair not in seen]
    if absent and row["generate_combinations"]:
        shown = ", ".join(f"{c} / {s}" if s else c for c, s in absent[:5])
        more = f" (+{len(absent) - 5} more)" if len(absent) > 5 else ""
        errors.append(
            f"{len(absent)} of {len(expected)} combination(s) have no child SKU "
            f"— {shown}{more}. Every colour x size this product generates needs "
            f"a line.")

    repeated = _repeated(row["combinations"])
    if repeated:
        errors.append(
            f"child SKU(s) {repeated} are used for more than one combination — "
            f"each combination takes its own")


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

        variants = []
        for offset, values in enumerate(rows[1:], start=2):
            variant = _read_variant(values, mapping, sheet=sheet, row_number=offset)
            if variant:
                variants.append(variant)

        legacy = _detect_legacy_layout(sheet, variants)
        if legacy:
            result.errors.append(legacy)
            continue

        parsed_rows = [_merge(group, unknown)
                       for group in _group_by_sku(variants).values()]

        _check_attribute_columns(sheet, mapping, parsed_rows, result)
        result.rows.extend(parsed_rows)

    workbook.close()

    duplicates = _duplicate_skus(result.rows)
    for row in result.rows:
        if row["sku"] in duplicates:
            row["errors"].append(
                f"SKU {row['sku']!r} appears on more than one sheet — a product "
                f"is one SKU, and its lines all belong on the same sheet")

    log.info("parsed %s — %d product(s) from %d variant line(s), %d valid, "
             "%d sheet error(s)", result.source_file, len(result.rows),
             sum(len(r["source_rows"]) for r in result.rows), result.valid_rows,
             len(result.errors))
    return result


def _group_by_sku(variants: list[dict]) -> dict[str, list[dict]]:
    """SKU -> its variant lines, in first-seen order.

    Grouped across the WHOLE sheet rather than over consecutive runs, so a
    product whose lines are split up — sorted by colour, or one appended
    later — still assembles. Nothing downstream depends on the order lines
    appear in, only on which product they belong to.

    A line with no SKU cannot belong to a product, so it gets a group of its
    own and _validate fails it by name. Bundling them together would report one
    error for what may be several unrelated mistakes.
    """
    groups: dict[str, list[dict]] = {}
    for variant in variants:
        key = variant["sku"] or f"\0blank-row-{variant['source_row']}"
        groups.setdefault(key, []).append(variant)
    return groups


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

    Only reported when the sheet actually uses sizes. A hard-hat sheet with an
    empty Size column has no size attribute to name and nothing to fix.
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
            f"'Product Attributes Type' column immediately before the Size "
            f"column and put 'Size' in every row. Assuming 'Size' for now.")
    elif any(SIZE_COLUMN_WRONG in row["warnings"] for row in rows):
        wrong = sum(1 for row in rows if SIZE_COLUMN_WRONG in row["warnings"])
        result.sheet_warnings.append(
            f"{sheet}: the SECOND 'Product Attributes Type' column (the one "
            f"just before Size) names the size attribute, but on {wrong} "
            f"product(s) it repeats the colour attribute instead. Put 'Size' in "
            f"that column on every row. Assuming 'Size' for now — left as it "
            f"is, both attributes would share one name and every size would be "
            f"added as a colour.")


def _unique(values) -> list[str]:
    """First-seen order, duplicates dropped."""
    return list(dict.fromkeys(values))


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
