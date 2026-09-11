"""Parse the workbook's Configurator Setup sheet — locations and areas.

    xlsx -> app.parser -> (product rows | CONFIGURATOR rows) -> runner

Phase 2 of the upload. Phase 1 creates products; this decorates a product that
already exists with the Picario configurator data the storefront renders: where
on the garment a logo can be placed, and what each of those spots looks like.

    Product name  SKU     Location Name  Picario RefrenceId  Picario Name             Thumbnail Name
    G347LA        G347LA  Front          G347LA-Front.pfs    L-chest-upper-right-...  L-chest-upper-right-....png
    G347LA        G347LA  Front                              K-chest-upper-left-...   K-chest-upper-left-....png
    G347LA        G347LA  Back           G347LA-Back.pfs     A-back-upper-...         A-back-upper-....png

THE GROUPING IS (SKU, LOCATION NAME), AND IT IS TWO DEEP. A product has
locations; a location has areas. The first line of a location carries its
Picario reference and the lines after it leave that cell blank — they are more
areas on the same location, not more locations. Reading a blank reference as
"another location called Front" would create a duplicate location and split its
areas across the two, so the blank cell is what joins a line to the group above
it.

WHAT THE COLUMNS ACTUALLY BECOME, verified against the plugin
(Aperture.CSPConfigurator) on the live admin:

    Location Name       -> Location.LocationName
    Picario RefrenceId  -> Location.ImageName ("G347LA-Front.pfs"), which the
                           admin checks against Picario before it can be saved
    Picario Name        -> Area.ImageIndex — an INTEGER, not the string. The id
                           comes back from the admin with the saved location, as
                           SceneAreas [{Id: 1, Name: "K-chest-upper-left-..."}],
                           so the name is matched against what Picario offers
                           for that scene and is never computed here.
    Thumbnail Name      -> Area.ImageName ("/thumbnails/<name>"), chosen from the
                           ~190 thumbnails the admin lists.

Which is why this module resolves NOTHING about those last two: it reads them,
checks the sheet is self-consistent, and leaves the matching to the page object,
which can see what the admin is actually offering. The same rule the colour
swatches follow, for the same reason — an id invented here would put a
plausible-looking area on the wrong part of the garment, and nobody would see it
until a customer did.

'Configurator Areas' and 'Add new location' are instructions to a human driving
the admin ("Select", "Yes"), not data. They are recognised so that they do not
trip the unknown-column guard, and then ignored.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# One definition of "what does this cell say". app.parser imports this module
# lazily (inside parse_workbook), so the dependency runs one way only.
from app.parser import _flag, _norm, _text

log = logging.getLogger("configurator")

#: Header text -> field. The reference column is misspelled "RefrenceId" in the
#: client's template; the correct spelling is accepted too, so that fixing the
#: typo in a later workbook does not break the parse.
HEADER_MAP: dict[str, str] = {
    "product name": "product_name",
    "sku": "sku",
    "location name": "location_name",
    "picario refrenceid": "reference_id",
    "picario referenceid": "reference_id",
    "picario reference id": "reference_id",
    "picario name": "picario_name",
    "thumbnail name": "thumbnail",
    "save product": "save_product",
    # Written by xlsxCreator, read back when a report is re-uploaded to finish
    # a batch. Without these the report's own two columns would fail every row
    # of it as unrecognised.
    "status": "status",
    "note": "note",
}

#: Columns that tell a human where to click. Recognised so the silent-drop guard
#: does not fire on them, then dropped.
NAVIGATION_HEADERS = {"configurator areas", "add new location"}

#: The template carries "Save" TWICE, spelled identically, and tells the two
#: apart by position exactly as it does the two "Product Attributes Type"
#: columns: the first saves the location, the second saves its areas.
SAVE_HEADER = "save"

#: What the admin's thumbnail dropdown lists its options as.
THUMBNAIL_PREFIX = "/thumbnails/"

#: A sheet is this shape when it names both of these. Detection is by HEADER,
#: never by sheet title — "Configurator Setup" is what this client happens to
#: call the tab, and a sheet carrying these columns means the same thing
#: whatever it is named.
REQUIRED_HEADERS = {"location name"}
REFERENCE_HEADERS = {"picario refrenceid", "picario referenceid",
                     "picario reference id"}

#: Headers that belong to phase 2 and to nothing else. Their leftmost column is
#: where the configurator block of a COMBINED sheet begins — see
#: app.parser._parse_combined, which uses that index to decide which half of the
#: sheet each reader owns. "Save" is not here on purpose: the product template
#: has a "Save Product" of its own, and the combined sheet carries both, so a
#: bare "Save" is only unambiguous once it is already past this boundary.
CONFIGURATOR_ONLY_HEADERS = (NAVIGATION_HEADERS | REQUIRED_HEADERS
                             | REFERENCE_HEADERS
                             | {"picario name", "thumbnail name"})


def block_starts_at(header_row: tuple) -> int | None:
    """Index of the first phase-2 column, or None if this sheet has none.

    A combined sheet is written as the product template followed by the
    configurator one, exactly as the two "Product Attributes Type" columns and
    the two "Save" columns are told apart: by position. So the boundary is a
    column index, and everything about who owns which column follows from it —
    including the duplicated "Save Product" header, whose first occurrence is
    phase 1's and whose second is phase 2's, with no special case needed.
    """
    for index, cell in enumerate(header_row):
        if _norm(cell) in CONFIGURATOR_ONLY_HEADERS:
            return index
    return None


def looks_like_configurator(header_row: tuple) -> bool:
    """Is this worksheet the configurator template rather than the product one?

    Both templates carry Product name and SKU, so those cannot tell them apart.
    Location Name and the Picario reference appear in one and not the other, and
    a sheet with both is unambiguous.
    """
    seen = {_norm(cell) for cell in header_row if cell}
    return bool(REQUIRED_HEADERS <= seen and seen & REFERENCE_HEADERS)


def map_headers(header_row: tuple) -> tuple[dict[int, str], list[str]]:
    """Column index -> field, plus the headers we did not recognise."""
    mapping: dict[int, str] = {}
    unknown: list[str] = []
    saves = 0

    for index, cell in enumerate(header_row):
        header = _norm(cell)
        if not header:
            continue
        if header in NAVIGATION_HEADERS:
            continue
        if header == SAVE_HEADER:
            saves += 1
            mapping[index] = "save_location" if saves == 1 else "save_areas"
        elif header in HEADER_MAP:
            mapping[index] = HEADER_MAP[header]
        else:
            unknown.append(str(cell).strip())
    return mapping, unknown


def thumbnail_path(name: str) -> str:
    """The sheet writes "A-front-hard-hat.png"; the admin lists it as
    "/thumbnails/A-front-hard-hat.png".

    Prefixing is a spelling change, not a resolution — the name still has to
    exist in the dropdown, and the page object is what checks that.
    """
    name = (name or "").strip()
    if not name or name.startswith(THUMBNAIL_PREFIX):
        return name
    return THUMBNAIL_PREFIX + name.lstrip("/")


#: A single space immediately before the file extension — " .pfs".
_SPACE_BEFORE_EXTENSION = re.compile(r" (\.[A-Za-z0-9]+)$")


def reference_alternatives(reference_id: str) -> list[str]:
    """Other spellings of a Picario reference worth TRYING, best first.

    Exactly one candidate today: the same name with one space removed from
    immediately before the extension. "280-HP542R F WE .pfs" -> "…F WE.pfs".

    THIS IS A CANDIDATE, NOT A CORRECTION, and the distinction is the whole
    safety argument. The two spellings are not the same asset with a typo — on
    the client's own store they are two real Picario scenes with different
    contents:

        280-HP542R F WE .pfs   ->  Full Color, Front logo
        280-HP542R F WE.pfs    ->  Full Color, A-front-hard-hat

    So a caller may only switch to one of these once it has SEEN that the
    alternative scene contains the spot the workbook asked for and the written
    one does not. Blindly trimming the space would silently move a logo onto a
    different render; verifying first cannot, because the spot name has to match
    exactly. See ConfiguratorPage.ensure_location, which does the verifying, and
    which reports every switch rather than quietly absorbing it — an alternative
    that keeps working is a sheet nobody ever fixes.

    Deliberately one space and one position. Collapsing all whitespace, or
    trying the reverse (adding a space), would widen the candidate set well past
    what the evidence supports.
    """
    reference = (reference_id or "").strip()
    match = _SPACE_BEFORE_EXTENSION.search(reference)
    if not match:
        return []
    return [reference[:match.start()] + match.group(1)]


def _read_line(values: tuple, mapping: dict[int, str], *, sheet: str,
               row_number: int) -> dict | None:
    """One worksheet line -> one area record, or None if the line is blank.

    A transcription, like app.parser._read_variant: nothing is judged here,
    because most of the questions worth asking ("does this location have a
    reference?") are only answerable once the lines are grouped.

    Cell text is stripped at the ends only. Internal spacing is left exactly as
    written, because it is load-bearing: the client's own references include
    "280-HP542R F WE .pfs", and Picario knows that scene by that name.
    """
    raw = {field: values[i] for i, field in mapping.items() if i < len(values)}
    if not any(_text(v) for v in raw.values()):
        return None

    return {
        "sku": _text(raw.get("sku")),
        "product_name": _text(raw.get("product_name")) or _text(raw.get("sku")),
        "location_name": _text(raw.get("location_name")),
        "reference_id": _text(raw.get("reference_id")),
        "picario_name": _text(raw.get("picario_name")),
        "thumbnail": _text(raw.get("thumbnail")),
        "save_location": _flag(raw.get("save_location"), default=True),
        "save_areas": _flag(raw.get("save_areas"), default=True),
        "save_product": _flag(raw.get("save_product"), default=True),
        "status": _text(raw.get("status")),
        "source_sheet": sheet,
        "source_row": row_number,
    }


def parse_sheet(sheet: str, worksheet_rows: list[tuple]) -> tuple[list[dict], list[str]]:
    """A configurator worksheet -> one row per product, plus sheet errors.

    Rows come out in the shape the runner drives:

        {"kind": "configurator", "sku": "G347LA", "locations": [
            {"name": "Front", "reference_id": "G347LA-Front.pfs", "areas": [
                {"picario_name": "...", "thumbnail": "/thumbnails/....png",
                 "source_row": 2}]}]}
    """
    return parse_lines(sheet, worksheet_rows, worksheet_rows[0])


def parse_lines(sheet: str, worksheet_rows: list[tuple], header: tuple, *,
                require_location: bool = False) -> tuple[list[dict], list[str]]:
    """parse_sheet, with the header row supplied and blank lines optionally skipped.

    A COMBINED sheet hands in a header row with the product half masked out, so
    that this reader sees only its own columns and reports none of phase 1's as
    unrecognised.

    `require_location` is the other half of that. On a combined sheet every line
    carries a SKU — it is the product's line as well as the location's — so the
    "is this line blank?" test in _read_line, which asks whether ANY mapped cell
    is filled, would read a product's fifth size as a location with no name. A
    line joins the configurator only if it says something about a location.
    """
    mapping, unknown = map_headers(header)
    errors: list[str] = []

    if "sku" not in mapping.values():
        errors.append(
            f"{sheet}: no 'SKU' column — found {[str(c) for c in header if c]}")
        return [], errors

    lines = []
    for offset, values in enumerate(worksheet_rows[1:], start=2):
        line = _read_line(values, mapping, sheet=sheet, row_number=offset)
        if not line:
            continue
        if require_location and not _says_anything_about_a_location(line):
            continue
        lines.append(line)

    # Grouped across the whole sheet rather than over consecutive runs, so a
    # product whose lines were split up still assembles. A line with no SKU
    # cannot belong to a product, so it gets a group of its own and _validate
    # fails it by name rather than bundling several unrelated mistakes together.
    groups: dict[str, list[dict]] = {}
    for line in lines:
        key = line["sku"] or f"\0blank-row-{line['source_row']}"
        groups.setdefault(key, []).append(line)

    rows = [_merge(group, unknown) for group in groups.values()]
    log.info("%s: %d product(s) from %d line(s)", sheet, len(rows), len(lines))
    return rows, errors


#: The fields that make a line a LOCATION's line rather than a product's. SKU
#: and product name are deliberately absent: on a combined sheet they are on
#: every line, including the ones that are only a size.
_LOCATION_FIELDS = ("location_name", "reference_id", "picario_name", "thumbnail")


def _says_anything_about_a_location(line: dict) -> bool:
    return any(line.get(field) for field in _LOCATION_FIELDS)


def _merge(lines: list[dict], unknown_headers: list[str]) -> dict:
    """A product's configurator lines -> the one row the runner drives."""
    first = lines[0]
    row: dict[str, Any] = {
        "kind": "configurator",
        "sku": first["sku"],
        "product_name": first["product_name"],
        "locations": [],
        "save_product": first["save_product"],
        "source_sheet": first["source_sheet"],
        "source_row": first["source_row"],
        "source_rows": [line["source_row"] for line in lines],
        "errors": [],
        "warnings": [],
    }

    # Location order is first-seen, and an area's order within its location is
    # the sheet's own order: the plugin stores a DisplayOrder per area, so the
    # sequence the sheet is written in is the answer to "which comes first".
    by_name: dict[str, dict] = {}
    for line in lines:
        name = line["location_name"]
        location = by_name.get(name)
        if location is None:
            location = {"name": name, "reference_id": "", "references": [],
                        "areas": [], "source_row": line["source_row"],
                        "save_location": line["save_location"],
                        "save_areas": line["save_areas"]}
            by_name[name] = location
            row["locations"].append(location)
        if line["reference_id"]:
            location["references"].append((line["reference_id"], line["source_row"]))
            if not location["reference_id"]:
                location["reference_id"] = line["reference_id"]
        location["areas"].append({
            "picario_name": line["picario_name"],
            "thumbnail": thumbnail_path(line["thumbnail"]),
            "thumbnail_asked": line["thumbnail"],
            "source_row": line["source_row"],
        })

    # A re-uploaded report: main.partition() reads this and leaves the row
    # alone if it is already done, so a batch that stopped halfway finishes
    # rather than reconfiguring what it already configured.
    status = next((line["status"] for line in lines if line["status"]), "")
    if status:
        row["status"] = status

    _validate(row, unknown_headers)
    for location in row["locations"]:
        location.pop("references", None)     # working state, not part of the row
    return row


def _validate(row: dict, unknown_headers: list[str]) -> None:
    """Everything wrong with this product's configurator data, all at once."""
    errors = row["errors"]

    if not row["sku"]:
        errors.append("SKU is required")

    # The same silent-drop guard app.parser applies. A column nobody modelled is
    # very likely a field this pipeline cannot set, and configuring the product
    # without it produces a wrong storefront and a clean report.
    if unknown_headers:
        errors.append(
            f"unrecognised column(s) {unknown_headers} — this automation only "
            f"models the location and area columns, and will not configure a "
            f"product from a sheet it does not fully understand")

    if not row["locations"]:
        errors.append("no locations are listed")
        return

    for location in row["locations"]:
        where = f"location {location['name']!r}" if location["name"] else "a location"
        at = [area["source_row"] for area in location["areas"]]

        if not location["name"]:
            errors.append(f"row(s) {at} give an area with no Location Name")

        # Exactly one reference per location. None means there is nothing to
        # create it from; two different ones means the sheet contradicts itself
        # about which Picario scene this location is, and nothing here knows
        # which of the two answers to discard.
        distinct = list(dict.fromkeys(ref for ref, _ in location["references"]))
        if not distinct:
            errors.append(
                f"{where} has no Picario RefrenceId — the first line of a "
                f"location must carry one (row(s) {at})")
        elif len(distinct) > 1:
            shown = "; ".join(f"{ref!r} on row {n}"
                              for ref, n in location["references"])
            errors.append(
                f"{where} is given {len(distinct)} different Picario "
                f"references: {shown}. One location is one Picario scene.")

        for area in location["areas"]:
            if not area["picario_name"]:
                errors.append(
                    f"row {area['source_row']} ({where}) has no Picario Name")
            if not area["thumbnail"]:
                errors.append(
                    f"row {area['source_row']} ({where}) has no Thumbnail Name")

        # The plugin refuses this itself — "Cannot have multiple areas setup for
        # same Picario Name" — but only at the end of a save, once the location
        # already exists. Catching it here costs nothing and leaves nothing
        # half-made behind.
        seen: dict[str, list[int]] = {}
        for area in location["areas"]:
            if area["picario_name"]:
                seen.setdefault(area["picario_name"], []).append(area["source_row"])
        for name, rows_at in seen.items():
            if len(rows_at) > 1:
                errors.append(
                    f"{where} uses Picario Name {name!r} on rows {rows_at} — one "
                    f"location cannot have two areas on the same spot")
