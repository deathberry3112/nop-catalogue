"""Colour code resolution: which colour owns which code in a row's SKUs.

    from app.colour import resolve
    resolution = resolve(row)
    resolution.codes        -> {"Yellow": "YW", ...}
    resolution.tiers        -> {"Yellow": "map", "Lime": "llm", ...}

Three tiers, cheapest and most trustworthy first — see app/colour/resolver.py.

Separately, and on the same principle of never guessing:

    from app.colour import swatches_for_row
    swatches, problems = swatches_for_row(row, options)

which turns the sheet's "Color Swatch" column into swatch ids this admin will
accept. A different question with a different answer — a code is derived from
the row's own child SKUs, a swatch is asked for by the workbook and named by
the catalogue — so it lives in its own module. See app/colour/swatch.py.
"""

from app.colour.resolver import Resolution, Tier, resolve
from app.colour.swatch import Choice, resolve_swatches, swatches_for_row

__all__ = ["Resolution", "Tier", "resolve", "Choice", "resolve_swatches",
           "swatches_for_row"]
