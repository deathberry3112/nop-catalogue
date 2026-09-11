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
reported, never ignored. An unexpected attribute column must not be dropped in
silence — the product would be created without it, the combinations generated
over one dimension too few, and the report would still say "Added". A wrong
catalogue with a clean report is the worst outcome available here, so an unknown
column is a row-level error and the row does not run.

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
    "generate combinations": "generate_combinations",
    "allow out of stock": "allow_out_of_stock",
    "child sku varient": "combinations",
    "child sku variant": "combinations",
    "save product": "save_product",
    "status": "status",
    "note": "note",
}

ATTRIBUTE_TYPE_HEADER = "product attributes type"

#: ATTRIBUTES ARE POSITIONAL, NOT NAMED, and this is the change that lets a
#: sheet carry attributes nobody wrote code for. A "Product Attributes Type"
#: column names an attribute; the next column along holds that attribute's
#: values; a column after that called "<values> Swatch" holds its swatches:
#:
#:     Product Attributes Type | Color         | Color Swatch | Product Attributes Type | Size
#:     Product Attributes Type | Sign Material |              | Product Attributes Type | Sign Size
#:
#: Colour and size are no longer special cases in the reader — they are simply
#: the two attributes the clothing sheets happen to use. What stays specific to
#: colour is downstream: swatch resolution and the legacy colour-code inference,
#: both of which only make sense for a colour.
SWATCH_SUFFIX = " swatch"

#: Value columns a sheet may carry WITHOUT a "Product Attributes Type" column in
#: front of them, and the attribute name to assume when it does. The old sheets
#: leave the second type column off entirely — SIZE_COLUMN_MISSING below is the
#: warning for exactly that — so dropping this fallback would refuse workbooks
#: that parse today.
IMPLIED_ATTRIBUTES: dict[str, str] = {
    "color": "Color",
    "colour": "Color",
    "size": "Size",
    "sizes": "Size",
}

#: The attribute a swatch column and the colour tiers apply to.
COLOUR_ATTRIBUTE_NAMES = {"color", "colour"}

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

#: Columns the RUN writes back, not ones the user fills in. A line carrying
#: nothing but these is not a variant: on a combined sheet, a report's phase-2
#: only line has a Status and a Note and no product data at all, and reading it
#: as a variant would answer one failed line with two failed rows.
BOOKKEEPING_FIELDS = ("status", "note")

#: Product-level fields, and how to name them when two lines disagree. The
#: wording is what the user sees, so it names the column, not the field.
SHARED_FIELDS: dict[str, str] = {
    "product_name": "Product name",
    "spec_value": "Attribute Specification Value",
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


def _map_headers(header_row: tuple
                 ) -> tuple[dict[int, str], list[str], list[dict]]:
    """Column index -> field name, the headers we did not recognise, and the
    attribute column groups in sheet order.

    Every "Product Attributes Type" column opens a group: it names the
    attribute, the next column holds that attribute's values, and a following
    "<values> Swatch" column holds its swatches. Any number of groups, in any
    order, under any names — which is what lets a Sign Boards sheet carry
    Sign Material and Sign Size without a line of code knowing those words.

    A value column with no type column in front of it still opens a group when
    the header is one this template has always understood (Color, Size), so
    sheets written before the second type column existed keep parsing.
    """
    headers = [_norm(cell) for cell in header_row]
    mapping: dict[int, str] = {}
    unknown: list[str] = []
    groups: list[dict] = []
    claimed: set[int] = set()

    def next_used(start: int) -> int | None:
        return next((i for i in range(start, len(headers)) if headers[i]), None)

    for index, header in enumerate(headers):
        if not header or index in claimed:
            continue

        if header == ATTRIBUTE_TYPE_HEADER:
            value_col = next_used(index + 1)
            if value_col is None:
                unknown.append(str(header_row[index]).strip())
                continue
            groups.append(_group(header_row, headers, index, value_col, claimed))
            continue

        if header in IMPLIED_ATTRIBUTES:
            groups.append(_group(header_row, headers, None, index, claimed))
            continue

        if header in HEADER_MAP:
            mapping[index] = HEADER_MAP[header]
        else:
            unknown.append(str(header_row[index]).strip())

    return mapping, unknown, groups


def _group(header_row: tuple, headers: list[str], type_col: int | None,
           value_col: int, claimed: set[int]) -> dict:
    """One attribute's columns: where its name, values and swatches live."""
    label = str(header_row[value_col]).strip()
    swatch_col = None

    following = next((i for i in range(value_col + 1, len(headers)) if headers[i]),
                     None)
    if following is not None and headers[following] == headers[value_col] + SWATCH_SUFFIX:
        swatch_col = following

    claimed.add(value_col)
    if type_col is not None:
        claimed.add(type_col)
    if swatch_col is not None:
        claimed.add(swatch_col)

    return {
        "type_col": type_col,
        "value_col": value_col,
        "swatch_col": swatch_col,
        # What to call the attribute when no type column names it. The column
        # header is the sheet's own word for it, so "Size" stays "Size".
        "default_name": IMPLIED_ATTRIBUTES.get(headers[value_col], label),
        "label": label,
        "is_colour": headers[value_col] in COLOUR_ATTRIBUTE_NAMES,
    }


def _read_variant(values: tuple, mapping: dict[int, str], *, sheet: str,
                  row_number: int, groups: list[dict] | None = None) -> dict | None:
    """One worksheet line -> one variant record, or None if the line is blank.

    Nothing is validated here. This is a transcription: the cells this line
    holds, split out of the sheet and into fields, so _detect_legacy_layout can
    judge the sheet's shape and _merge can group by SKU. Validation happens on
    the assembled product, because that is the level most of the questions are
    asked at ("do these colours and sizes cover every child SKU?").
    """
    groups = groups or []
    raw = {field: values[i] for i, field in mapping.items() if i < len(values)}

    def cell(index: int | None):
        return values[index] if index is not None and index < len(values) else None

    # One entry per attribute the SHEET declares, in sheet order, whether or not
    # this line fills it. Order is what makes a combination's identity stable.
    attributes = []
    for group in groups:
        attributes.append({
            "name": _text(cell(group["type_col"])) or group["default_name"],
            "declared": _text(cell(group["type_col"])),
            "values": _split(cell(group["value_col"])),
            "swatches": _split(cell(group["swatch_col"])),
            "has_type_column": group["type_col"] is not None,
            "is_colour": group["is_colour"],
            "default_name": group["default_name"],
            "has_swatch_column": group["swatch_col"] is not None,
        })

    if not any(_text(value) for field, value in raw.items()
               if field not in BOOKKEEPING_FIELDS) and not any(
            a["values"] or a["swatches"] for a in attributes):
        return None

    return {
        "sku": _text(raw.get("sku")),
        "product_name": _text(raw.get("product_name")) or _text(raw.get("sku")),
        "spec_value": _text(raw.get("spec_value")),
        "attributes": attributes,
        "combinations": _split(raw.get("combinations")),
        "generate_combinations": _flag(raw.get("generate_combinations")),
        "allow_out_of_stock": _flag(raw.get("allow_out_of_stock")),
        "save_product": _flag(raw.get("save_product"), default=True),
        "status": _text(raw.get("status")),
        "source_sheet": sheet,
        "source_row": row_number,
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
        packed = {attribute["name"].casefold(): len(attribute["values"])
                  for attribute in variant["attributes"]
                  if len(attribute["values"]) > 1}
        if len(variant["combinations"]) > 1:
            packed["child SKU"] = len(variant["combinations"])
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

    THE GENERAL SHAPE IS `attributes`, and everything else is derived from it:

        row["attributes"] -> [{"name": "Sign Material",
                               "values": ["Aluminum", "Plastic", ...],
                               "swatches": [], "is_colour": False}, ...]
        row["variants"]   -> [{"values": {"Sign Material": "Aluminum",
                                          "Sign Size": "7 x 10"},
                               "sku": "SN-B0014-A0710"}, ...]

    A variant states which child SKU belongs to which combination of attribute
    values, so combination_skus.combination_map() reads the pairing instead of
    inferring it. Lists of dicts rather than the {(values): sku} mapping the
    runner wants, because rows are JSON — persisted to the run store and handed
    to the browser — and a tuple key survives neither.

    COLOUR AND SIZE ALIASES are kept beside that (`colors`, `sizes`,
    `color_attr`, `size_attr`, and `color`/`size` on each variant) and are
    populated only when the sheet actually has such attributes. They are not
    legacy debt: colour is genuinely special downstream — it is the only
    attribute with swatches and the only one whose code can be inferred from a
    child SKU — so the colour machinery reads them and the generic runner reads
    `attributes`.
    """
    first = variants[0]
    names = [a["name"] for a in first["attributes"]]

    attributes: list[dict] = []
    for index, name in enumerate(names):
        seen: list[str] = []
        swatch_of: dict[str, list[str]] = {}
        for variant in variants:
            entry = variant["attributes"][index]
            if not entry["values"]:
                continue
            value = entry["values"][0]
            if value not in seen:
                seen.append(value)
            if entry["swatches"]:
                swatch_of.setdefault(value, []).append(entry["swatches"][0])
        attributes.append({
            "name": name,
            "values": seen,
            # One swatch per value, taken from the lines naming that value. Each
            # line carries its own pair, so a swatch cannot drift onto the wrong
            # value; a value whose lines disagree is reported by _validate.
            "swatches": [swatch_of[v][0] for v in seen if v in swatch_of],
            "_swatches_by_value": swatch_of,
            "is_colour": first["attributes"][index]["is_colour"],
            "has_type_column": first["attributes"][index]["has_type_column"],
            "header_default": first["attributes"][index]["default_name"],
            # Whether the SHEET carried a swatch column, not whether anything
            # filled it — the report has to write the columns back as it found
            # them or the round trip changes the template.
            "has_swatch_column": first["attributes"][index]["has_swatch_column"],
        })

    row: dict[str, Any] = {
        "sku": first["sku"],
        "product_name": first["product_name"],
        "spec_value": first["spec_value"],
        "attributes": attributes,
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

    # NAMES FIRST, THEN VARIANTS. A variant's values are keyed by attribute
    # name, so building them before a repeated name is repaired would collapse
    # two attributes onto one key and lose the first one's value.
    _name_the_size_attribute(row, attributes, names)

    for variant in variants:
        if not variant["combinations"]:
            continue
        stated = {names[i]: entry["values"][0]
                  for i, entry in enumerate(variant["attributes"])
                  if entry["values"]}
        row["variants"].append({
            "values": stated,
            "sku": variant["combinations"][0],
            "source_row": variant["source_row"],
        })

    _add_colour_aliases(row, attributes, names)

    status = next((v["status"] for v in variants if v["status"]), "")
    if status:
        row["status"] = status                 # a re-uploaded report: resume it

    _validate(row, variants, unknown_headers)
    for attribute in row["attributes"]:
        attribute.pop("_swatches_by_value", None)
    return row


def _name_the_size_attribute(row: dict, attributes: list[dict],
                             names: list[str]) -> None:
    """Fix a second attribute the sheet failed to name, or named as the first.

    The type columns are positional, so a second one repeating "Color" names the
    SAME attribute twice. Not a harmless typo: add_attribute is idempotent BY
    NAME, so the second pass would fetch the first attribute back and file "S/M"
    as a ninth colour. A blank cell already falls back to the column header; a
    cell repeating the previous attribute means the same thing, so it is treated
    the same way.

    Only ever applied to an attribute whose column header says what it is — the
    old Color/Size template. A sheet naming its own attributes ("Sign Size") has
    nothing to fall back to and a repeated name there is a plain error, reported
    by _validate rather than silently repaired.
    """
    for index, attribute in enumerate(attributes):
        if index == 0 or not attribute["values"]:
            continue
        default = IMPLIED_ATTRIBUTES.get(_norm(attribute["name"]))
        header_default = attribute.get("header_default")
        if not attribute["has_type_column"]:
            reason = SIZE_COLUMN_MISSING
        elif attribute["name"] in names[:index]:
            reason = SIZE_COLUMN_WRONG
        else:
            continue
        row["warnings"].append(reason)
        # Which attribute, not just that something was repaired — otherwise the
        # sheet-level banner fires once per attribute column on the sheet.
        row.setdefault("_repaired", {})[index] = reason
        repaired = header_default or default or "Size"
        if repaired == attribute["name"]:
            # Nothing to fall back to — the column header says the same thing
            # the type cell does. Saying "assuming X" when X is what it already
            # says would be a repair that repairs nothing; _validate reports the
            # duplicate instead.
            row["warnings"].pop()
            row.get("_repaired", {}).pop(index, None)
            continue
        attribute["name"] = repaired
        names[index] = repaired


def _add_colour_aliases(row: dict, attributes: list[dict],
                        names: list[str]) -> None:
    """Populate the colour/size view of this row, where it has one.

    The colour resolver, the swatch module and combination_skus all speak
    colours and sizes, because those are the things a child SKU encodes and the
    things a swatch attaches to. A sign board has neither, so it gets empty
    lists and none of that machinery engages.
    """
    colour = next((a for a in attributes if a["is_colour"]), None)
    # The "size" of a colour product is whatever second attribute it actually
    # fills. A product with no colour has neither: leaving these empty is what
    # keeps the colour tiers, the swatch resolver and the child-SKU inference
    # from engaging on a sheet about sign materials.
    sized = next((a for a in attributes if a is not colour and a["values"]),
                 None) if colour else None

    row["color_attr"] = colour["name"] if colour else (
        attributes[0]["name"] if attributes else "Color")
    row["colors"] = list(colour["values"]) if colour else []
    row["swatches"] = list(colour["swatches"]) if colour else []
    row["_swatches_by_colour"] = dict(colour["_swatches_by_value"]) if colour else {}
    row["size_attr"] = sized["name"] if sized else (
        attributes[1]["name"] if len(attributes) > 1 else "Size")
    row["sizes"] = list(sized["values"]) if sized else []

    for variant in row["variants"]:
        values = variant["values"]
        variant["color"] = values.get(colour["name"], "") if colour else ""
        variant["size"] = values.get(sized["name"]) if sized else None


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
            f"unrecognised column(s) {unknown_headers} — an attribute needs a "
            f"'Product Attributes Type' column naming it, immediately before "
            f"the column holding its values, and this automation will not "
            f"create a product from a sheet it does not fully understand")

    # Two attribute columns naming ONE attribute. add_attribute is idempotent
    # by name, so the second column would fetch the first attribute back and
    # add its values to that — one attribute holding both columns' values, and
    # a combination grid that is the wrong shape. Caught here because the
    # symptom downstream ("Every Sign Size x Sign Size needs a line") describes
    # the consequence rather than the cause.
    counts: dict[str, int] = {}
    for attribute in row["attributes"]:
        counts[attribute["name"]] = counts.get(attribute["name"], 0) + 1
    for name, count in counts.items():
        if count > 1:
            errors.append(
                f"{count} attribute columns are named {name!r} — each "
                f"'Product Attributes Type' column must name a different "
                f"attribute, or their values would be added to the same one")

    # These columns describe the PRODUCT, so every line of it has to agree. Two
    # lines sharing a SKU and disagreeing about the name (or about whether to
    # save it) do not describe a product this can create — one of the two
    # answers would have to be discarded, and nothing here knows which.
    _check_agreement(row, variants)

    # Each value's swatch comes off its own line now, so a disagreement is a
    # real contradiction rather than the old column-alignment guess.
    row.pop("_swatches_by_colour", None)
    for attribute in row["attributes"]:
        for value, given in (attribute.get("_swatches_by_value") or {}).items():
            distinct = _unique(given)
            if len(distinct) > 1:
                errors.append(
                    f"{value!r} is given {len(distinct)} different swatches "
                    f"{distinct} — one value has one swatch")
        if attribute["swatches"] and len(attribute["swatches"]) != len(attribute["values"]):
            warnings.append(
                f"{len(attribute['swatches'])} swatch(es) for "
                f"{len(attribute['values'])} {attribute['name']} value(s)")

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

    # The attribute NAMES are product-level too: the type columns are what tell
    # add_attribute which attribute to map, and two lines naming different ones
    # would configure two different products.
    for index in range(len(row["attributes"])):
        named: dict[str, list[int]] = {}
        for variant in variants:
            entry = variant["attributes"][index]
            named.setdefault(entry["name"], []).append(variant["source_row"])
        if len(named) > 1:
            shown = "; ".join(f"{value!r} on row(s) {rows}"
                              for value, rows in named.items())
            row["errors"].append(
                f"the lines for {row['sku'] or 'this SKU'} disagree about "
                f"attribute {index + 1}'s name: {shown}. Every line of a "
                f"product must repeat the same values in that column.")


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
    import itertools

    errors = row["errors"]

    if not row["variants"]:
        if row["generate_combinations"]:
            errors.append("Generate Combinations is set but no child SKUs are listed")
        return

    filled = [a for a in row["attributes"] if a["values"]]
    if not filled:
        declared = [a["name"] for a in row["attributes"]]
        errors.append(
            f"no attribute values are listed — this product has child SKUs but "
            f"every attribute column ({declared or 'none declared'}) is empty")
        return

    names = [a["name"] for a in filled]

    # An attribute is set on every line of a product or on none of them. Half
    # and half leaves some lines describing an N-dimensional grid and the rest
    # an (N-1)-dimensional one, and no single reading covers both.
    for name in names:
        bare = [v["source_row"] for v in row["variants"]
                if not v["values"].get(name)]
        if bare and len(bare) != len(row["variants"]):
            errors.append(
                f"row(s) {bare} leave the {name} cell empty while the rest of "
                f"this product sets one — a product sets an attribute on every "
                f"line or on none")
            return
        if bare:
            errors.append(f"row(s) {bare} give a child SKU with no {name}")
            return

    def where(key: tuple[str, ...]) -> str:
        return " / ".join(k for k in key if k)

    seen: dict[tuple[str, ...], list[str]] = {}
    for variant in row["variants"]:
        key = tuple(variant["values"].get(name, "") for name in names)
        seen.setdefault(key, []).append(variant["sku"])

    for key, skus in seen.items():
        if len(skus) > 1:
            errors.append(
                f"{len(skus)} child SKUs for {where(key)}: {skus}. One "
                f"combination takes one child SKU.")

    # Only when combinations are actually being generated. A product that is
    # not generating a grid has no grid to fill, and blocking it over a gap in
    # one it will never build would refuse a row the runner handles fine.
    expected = list(itertools.product(*(a["values"] for a in filled)))
    absent = [key for key in expected if key not in seen]
    if absent and row["generate_combinations"]:
        shown = ", ".join(where(key) for key in absent[:5])
        more = f" (+{len(absent) - 5} more)" if len(absent) > 5 else ""
        errors.append(
            f"{len(absent)} of {len(expected)} combination(s) have no child SKU "
            f"— {shown}{more}. Every {' x '.join(names)} this product generates "
            f"needs a line.")

    repeated = _repeated(row["combinations"])
    if repeated:
        errors.append(
            f"child SKU(s) {repeated} are used for more than one combination — "
            f"each combination takes its own")


#: Headers that only the PRODUCT template has. One of these to the left of the
#: configurator block is what tells a combined sheet from a phase-2 sheet, which
#: also carries Product name and SKU.
PRODUCT_ONLY_HEADERS = {
    "attribute specification value", ATTRIBUTE_TYPE_HEADER,
    "generate combinations", "allow out of stock",
    "child sku varient", "child sku variant",
} | set(IMPLIED_ATTRIBUTES)

#: Columns a combined sheet's two halves BOTH read. Everything else is decided
#: by which side of the boundary it falls on.
SHARED_COMBINED_HEADERS = {"product name", "sku", "status", "note"}


def _combined_boundary(header_row: tuple) -> int | None:
    """Where phase 2's columns start on a combined sheet, or None if it is not one.

    A phase-2 sheet also carries Product name and SKU, so the presence of
    configurator columns is not enough: what makes a sheet COMBINED is product
    columns to the LEFT of them.
    """
    from app import configurator

    edge = configurator.block_starts_at(header_row)
    if edge is None:
        return None
    left = {_norm(cell) for cell in header_row[:edge] if cell}
    return edge if left & PRODUCT_ONLY_HEADERS else None


def _mask(header_row: tuple, keep) -> tuple:
    """The same header row with every column `keep` rejects blanked out.

    Blanked rather than sliced so the indexes still line up with the worksheet's
    cells — both readers then work on full-width lines and neither needs to know
    the other exists.
    """
    return tuple(cell if keep(index, _norm(cell)) else None
                 for index, cell in enumerate(header_row))


def _parse_combined(sheet: str, worksheet_rows: list[tuple], edge: int,
                    result: ParseResult) -> list[dict]:
    """A sheet carrying BOTH templates -> one row per product, locations attached.

    ONE ROW, NOT TWO, and that is the decision the rest of this follows from.
    The alternative — a product row and a configurator row per SKU — would put
    the two phases on separate lines of the report, so re-uploading it would no
    longer be the combined template the user wrote. One row round-trips, runs
    the phases in the only order that works (the configurator needs the product
    to exist), and gives the console one line per product.

    The two halves are read by the two readers that already exist, each shown
    only its own columns. What they do NOT share is line granularity: phase 1
    writes one line per variant and phase 2 one line per area, and a product
    with seven sizes and four areas has seven lines of which four also say
    something about a location. Neither reader is told about the other's blanks
    — see configurator.parse_lines(require_location=True).
    """
    from app import configurator

    header = worksheet_rows[0]
    product_header = _mask(
        header, lambda i, h: i < edge or h in SHARED_COMBINED_HEADERS)
    configurator_header = _mask(
        header, lambda i, h: i >= edge or h in SHARED_COMBINED_HEADERS)

    mapping, unknown, groups = _map_headers(product_header)
    if "sku" not in mapping.values():
        result.errors.append(
            f"{sheet}: no 'SKU' column — found {[str(c) for c in header if c]}")
        return []
    if unknown:
        log.warning("%s: unrecognised column(s) %s", sheet, unknown)

    variants = []
    for offset, values in enumerate(worksheet_rows[1:], start=2):
        variant = _read_variant(values, mapping, sheet=sheet,
                                row_number=offset, groups=groups)
        if variant:
            variants.append(variant)

    legacy = _detect_legacy_layout(sheet, variants)
    if legacy:
        result.errors.append(legacy)
        return []

    rows = [_merge(group, unknown) for group in _group_by_sku(variants).values()]
    _check_attribute_columns(sheet, groups, rows, result)
    for row in rows:
        row.pop("_repaired", None)

    configurator_rows, errors = configurator.parse_lines(
        sheet, worksheet_rows, configurator_header, require_location=True)
    result.errors.extend(errors)

    by_sku = {row["sku"]: row for row in rows if row["sku"]}
    for configured in configurator_rows:
        product = by_sku.get(configured["sku"])
        if product is None:
            # Only reachable for a line with NO SKU, since the SKU column is
            # shared and any line that fills it produces a product row to attach
            # to. Kept as a phase-2 row so it fails on its own terms ("SKU is
            # required") instead of disappearing — a line that says where a logo
            # goes and never says on what is the kind of mistake a report has to
            # name.
            rows.append(configured)
            continue
        product["locations"] = configured["locations"]
        # The configurator's own validation, carried onto the row that now owns
        # the locations. Dropping it would let a location with no Picario
        # reference through on a sheet that merely also creates the product.
        product["errors"].extend(configured["errors"])
        product["warnings"].extend(configured["warnings"])

    log.info("%s: %d combined row(s), %d with locations", sheet, len(rows),
             sum(1 for row in rows if row.get("locations")))
    return rows


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

        # Phase 2's sheet is a different template answering a different
        # question, so it is routed rather than forced through the product
        # reader — which would reject every line of it as unrecognised columns.
        # Imported here so the dependency runs one way: configurator imports
        # this module's cell helpers, and this module reaches for it only when
        # a sheet turns out to need it.
        from app import configurator
        edge = _combined_boundary(rows[0])
        if edge is not None:
            result.rows.extend(_parse_combined(sheet, rows, edge, result))
            continue
        if configurator.looks_like_configurator(rows[0]):
            parsed_rows, sheet_errors = configurator.parse_sheet(sheet, rows)
            result.errors.extend(sheet_errors)
            result.rows.extend(parsed_rows)
            continue

        mapping, unknown, groups = _map_headers(rows[0])

        if "sku" not in mapping.values():
            result.errors.append(
                f"{sheet}: no 'SKU' column — found {[str(c) for c in rows[0] if c]}")
            continue
        if unknown:
            log.warning("%s: unrecognised column(s) %s", sheet, unknown)

        variants = []
        for offset, values in enumerate(rows[1:], start=2):
            variant = _read_variant(values, mapping, sheet=sheet,
                                    row_number=offset, groups=groups)
            if variant:
                variants.append(variant)

        legacy = _detect_legacy_layout(sheet, variants)
        if legacy:
            result.errors.append(legacy)
            continue

        parsed_rows = [_merge(group, unknown)
                       for group in _group_by_sku(variants).values()]

        _check_attribute_columns(sheet, groups, parsed_rows, result)
        for parsed in parsed_rows:
            parsed.pop("_repaired", None)
        result.rows.extend(parsed_rows)

    workbook.close()

    duplicates = _duplicate_skus(result.rows)
    for row in result.rows:
        if (row.get("kind") or "product", row["sku"]) in duplicates:
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


def _check_attribute_columns(sheet: str, groups: list[dict],
                             rows: list[dict], result: ParseResult) -> None:
    """Is every attribute this sheet uses actually named by a column?

    "Product Attributes Type" is spelled identically for every attribute and
    they are told apart by position, which makes them easy to get wrong and, in
    the old parser, quiet about it: a missing second column used to fall back to
    "Size" in silence, and a second column repeating "Color" made the runner
    file every size as a colour. The check below reports that instead.

    Reported once per sheet and in the user's vocabulary. The header they can
    search for is "Product Attributes Type"; "the size attribute column" names
    something that appears nowhere in their spreadsheet.

    Only for attributes the sheet actually fills. A hard-hat sheet with an empty
    Size column has no size attribute to name and nothing to fix.
    """
    for index, group in enumerate(groups):
        used = [row for row in rows
                if index < len(row["attributes"]) and row["attributes"][index]["values"]]
        if not used:
            continue
        example = used[0]
        label = group["label"] or f"attribute {index + 1}"

        if group["type_col"] is None:
            result.sheet_warnings.append(
                f"{sheet}: this sheet fills the {label!r} column (e.g. "
                f"{example['attributes'][index]['values']} on {example['sku']}) "
                f"but has no 'Product Attributes Type' column naming it. Add "
                f"one immediately before the {label!r} column and put the "
                f"attribute's name in every row. Assuming "
                f"{group['default_name']!r} for now.")
        elif any((row.get("_repaired") or {}).get(index) == SIZE_COLUMN_WRONG
                 for row in used):
            wrong = sum(1 for row in used
                        if (row.get("_repaired") or {}).get(index) == SIZE_COLUMN_WRONG)
            result.sheet_warnings.append(
                f"{sheet}: the 'Product Attributes Type' column just before "
                f"{label!r} should name that attribute, but on {wrong} "
                f"product(s) it repeats an earlier one. Put the attribute's own "
                f"name in that column on every row. Assuming "
                f"{group['default_name']!r} for now — left as it is, two "
                f"attributes would share one name and every {label} value would "
                f"be added to the wrong one.")


def _unique(values) -> list[str]:
    """First-seen order, duplicates dropped."""
    return list(dict.fromkeys(values))


def _repeated(values: list[str]) -> list[str]:
    """The entries appearing more than once, in first-seen order."""
    seen: dict[str, int] = {}
    for value in values:
        seen[value] = seen.get(value, 0) + 1
    return [value for value in dict.fromkeys(values) if seen[value] > 1]


def _duplicate_skus(rows: list[dict]) -> set[tuple[str, str]]:
    """The (kind, SKU) pairs claimed by more than one row.

    Counted PER KIND, because a product row and a configurator row naming the
    same SKU are not a duplicate — they are phase 1 and phase 2 of the same
    product, which is the normal way a workbook carrying both is written.
    Counting them together would fail every product the client configures.
    """
    seen: dict[tuple[str, str], int] = {}
    for row in rows:
        if row["sku"]:
            key = (row.get("kind") or "product", row["sku"])
            seen[key] = seen.get(key, 0) + 1
    return {key for key, count in seen.items() if count > 1}
