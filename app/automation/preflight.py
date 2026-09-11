"""Check a sheet's attribute names against the store BEFORE anything is created.

    unknown_attributes(["Color", "Sign Material"], known) -> [("Sign Material", ...)]

WHY THIS EXISTS AT ALL. Attributes live at two levels in nopCommerce. The
store-wide list under Catalog > Attributes > Product attributes defines the
NAMES; a product's own "Product attributes" tab maps that product to one of
them. ProductPage.add_attribute drives the second, and its dropdown can only
offer what the first already holds.

So a sheet naming an attribute nobody has created yet fails at _pick_kendo —
and it fails LATE, after the product has been created and saved, leaving a
half-configured product in the catalogue for somebody to find. The sheet is
readable, the row is valid, and the automation has already written to the
client's store before it discovers it cannot finish.

Checking the names up front turns that into a refusal that costs nothing: the
row is reported with the name it asked for and the product is never created.
Deliberately NOT a creation step — a typo in a workbook ("Sign Materal") would
otherwise add a permanent attribute every product in the store can see, and an
attribute nobody meant is far harder to notice than a row that refused to run.
Somebody adds the name once in the admin; every product using it then runs
untouched forever after.

Pure functions, no page object: what the store offers is read by
pages.product_attributes, and what to do about it is decided here.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

import library


class UnknownAttribute(library.StepError):
    """The sheet named an attribute this store does not define.

    Its own type rather than a plain StepError so app.errors can classify it by
    type instead of by wording. The message quotes the store's own attribute
    names, and a store with an attribute called "Colour" would otherwise be
    filed as a colour-resolution failure by the DATA_COLOUR pattern.
    """

#: How many of the store's own names to quote back. The whole list is dozens
#: long and lands in a spreadsheet cell; enough to recognise the spelling is
#: what helps, and the count says the rest are there.
SHOWN = 8


def _key(name: str) -> str:
    """The form two spellings of one attribute agree on.

    Case and runs of whitespace only. This is for FINDING A NEAR MISS to quote
    in the message — never for deciding a name matches, because add_attribute's
    dropdown pick is exact and case-sensitive. Treating "color" as "Color" here
    would let the row through to fail exactly where this module exists to stop
    it failing.
    """
    return re.sub(r"\s+", " ", name).strip().casefold()


def unknown_attributes(wanted: Sequence[str],
                       available: Iterable[str]) -> list[tuple[str, str]]:
    """[(name, what the store spells it instead)] for names the store lacks.

    The second element is "" unless the store holds a name that differs only in
    case or spacing, which is the overwhelmingly likely mistake and the one a
    bare "not found" is least helpful about.

    Order follows `wanted`, and a name asked for twice is reported once — the
    sheet's order is the order the runner configures in, so a message that
    follows it can be read against the columns.
    """
    known = set(available)
    near: dict[str, str] = {}
    for name in known:
        near.setdefault(_key(name), name)

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in wanted:
        if name in known or name in seen:
            continue
        seen.add(name)
        alternative = near.get(_key(name), "")
        out.append((name, alternative if alternative != name else ""))
    return out


def refusal(unknown: Sequence[tuple[str, str]], available: Iterable[str]) -> str:
    """The message a refused row carries, into the console and the report.

    It has to answer three things for somebody who did not write this code:
    which name was not found, what the store has instead, and what to do about
    it. The last one is the point — the fix is one action in the admin, and a
    message that does not say so reads as a bug in the automation.
    """
    parts = []
    for name, alternative in unknown:
        if alternative:
            parts.append(f"{name!r} (this store spells it {alternative!r} — the "
                         f"names must match exactly)")
        else:
            parts.append(repr(name))

    listed = sorted(available)
    shown = ", ".join(listed[:SHOWN])
    more = f" (+{len(listed) - SHOWN} more)" if len(listed) > SHOWN else ""

    subject = "no product attribute named" if len(parts) == 1 else \
              "no product attributes named"
    return (f"this store has {subject} {'; '.join(parts)}. Add it under "
            f"Catalog > Attributes > Product attributes, then run this row "
            f"again. The store currently has: {shown}{more}")
