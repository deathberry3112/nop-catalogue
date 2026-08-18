#!/usr/bin/env python3
"""Work out which combination SKU belongs to which colour/size, per row.

The payload gives combination SKUs as opaque strings — "CP-30-C100V YW",
"CP-V-200 LE 2XL" — and never says which colour "YW" is. This module recovers
that, without hardcoding a colour table and without assuming the Nth generated
combination is the Nth string in the payload.

    mapping = combination_map(row)
    mapping[("Yellow", None)]   -> "CP-30-C100V YW"
    mapping[("Lime", "2XL")]    -> "CP-V-200 LE 2XL"

Callers set a combination's SKU by reading what that combination actually *is*
off the grid ("Color: Yellow, Size: L") and looking it up here. Nothing depends
on ordering at any point.

WHY NOT JUST COMPUTE THE CODE FROM THE NAME
There is a rule that fits most of the data — the initial of each word plus the
last letter of the last word: Yellow->YW, White->WE, Grey->GY, Green->GN,
Hi-Vis Orange->HOE. It is tempting and it is wrong. The vest rows spell
"Hi-Viz Lime" as LE, not HLE, so a computed code would confidently produce a
SKU that belongs to no combination. Instead the rule is used only to *score*
candidates, and the answer must come out unique against the codes the payload
actually contains. A colour nobody anticipated works automatically as long as
its code is recognisable; one that isn't fails loudly and names itself.
"""

from __future__ import annotations

import re

# Colours whose code cannot be recovered from the name. Empty, and expected to
# stay that way — add an entry only when a row fails with "ambiguous", and only
# for the colour it names. This is per-colour, not per-row.
OVERRIDES: dict[str, str] = {}


class CombinationError(Exception):
    """The payload's combinations cannot be matched to its colours and sizes."""


def _words(name: str) -> list[str]:
    return [w for w in re.split(r"\s+", name.strip()) if w]


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _is_subsequence(needle: str, haystack: str) -> bool:
    it = iter(haystack)
    return all(c in it for c in needle)


def score(code: str, colour: str) -> int:
    """How well `code` fits `colour`. Higher is better; 0 means "not plausible".

    The signals are ordered by how much they actually pin the answer down. The
    first two carry the weight; the rest only break near-ties.
    """
    code_l = _letters(code)
    words = _words(colour)
    if not code_l or not words:
        return 0

    flat = _letters(colour)
    last_word = _letters(words[-1])
    initials = "".join(_letters(w)[:1] for w in words)

    total = 0
    # "Hi-Vis Orange" -> H + O + e
    if code_l == initials + last_word[-1:]:
        total += 100
    # "Hi-Viz Lime" -> L + e, dropping the qualifier entirely
    if code_l == last_word[:1] + last_word[-1:]:
        total += 80
    if _is_subsequence(code_l, flat):
        total += 30
    if code_l[:1] in {w[:1] for w in (_letters(x) for x in words) if w}:
        total += 10
    if code_l[-1:] == flat[-1:]:
        total += 10
    return total


def base_prefix(row: dict) -> str:
    """What to strip off each combination to leave the colour code (and size).

    Normally the row's SKU — the template's contract is that a child SKU is the
    parent SKU plus a colour code. When the sheet's SKU does not match, though,
    the combinations still agree with *each other*, and what they agree on is
    the real base: a row reading SKU "20-100ECO" against children
    "CP-20-100ECO C/CAF/S/SAF" is one mistyped cell, not four bad child SKUs.

    Recovering it that way is what lets the mismatch be a warning instead of a
    blocking error. Without it the prefix survives the split, every child SKU
    becomes its own "colour code", and the row fails deep inside the run with a
    product already created — strictly worse than refusing it up front.

    WHOLE TOKENS ONLY, and this is the whole risk of the function: the codes
    C, CAF, S, SAF share their first letters, so a character-wise common prefix
    would eat them and leave '', 'AF', '', 'AF' as the codes. That is silently
    wrong — a plausible mapping onto the wrong colours — which is the one
    outcome this module exists to rule out. Anything short of a full token
    agreeing across every combination falls back to the SKU.
    """
    sku = str(row.get("sku") or "")
    combinations = [c.strip() for c in (row.get("combinations") or []) if c.strip()]

    if not combinations:
        return sku
    if sku and all(c.upper().startswith(sku.upper()) for c in combinations):
        return sku
    if len(combinations) < 2:
        return sku          # one combination agrees with itself; nothing to learn

    shared: list[str] = []
    for tokens in zip(*(c.split() for c in combinations)):
        if len({t.upper() for t in tokens}) != 1:
            break
        shared.append(tokens[0])

    # Never consume every token of any combination: a code has to be left over.
    if not shared or min(len(c.split()) for c in combinations) <= len(shared):
        return sku
    return " ".join(shared)


def implied_size(row: dict) -> str | None:
    """The size every combination carries without writing it, or None.

    The template lets a row spell its children two ways:

        Vest      Sizes "OS"          CP-V-010 LE OS      the size is written
        HardHat   Sizes "NA"          CP-30-C100V YW      there is no size
        Eyewear   Sizes "OS"          CP-20-100 A         a size, left unwritten

    The third shape used to be a hard failure — "does not end in one of the
    row's sizes ['OS']" — because the splitter needs a trailing token to peel
    off. But when there is exactly ONE size, nothing needs peeling: 8 colours x
    1 size is 8 combinations and there are 8 child SKUs, so the pairing is
    forced. That is a derivation, not a guess, which is the whole reason this
    is allowed to be lenient.

    DECIDED PER ROW, NEVER PER COMBINATION, and that is the point of this
    function. Judging each SKU on its own would let a row where some carry the
    size and some do not resolve half one way and half the other — a plausible
    mapping onto the wrong sizes. So:

        every combination carries it   -> explicit, strip it as before
        none carry it, exactly 1 size  -> implicit, this returns that size
        none carry it, 2+ sizes        -> None; unknowable, and it must fail
        some carry it                  -> None; ambiguous, and it must fail

    The last two return None so the caller raises the same error it always did.
    """
    sizes = row.get("sizes") or []
    combinations = [c.strip() for c in (row.get("combinations") or []) if c.strip()]
    if len(sizes) != 1 or not combinations:
        return None

    wanted = sizes[0].casefold()
    carried = sum(1 for c in combinations if c.split()[-1].casefold() == wanted)
    return sizes[0] if carried == 0 else None


def size_aliases(row: dict) -> dict[str, str]:
    """Trailing SKU tokens that abbreviate one of the row's sizes.

    The Grainger sheets write the size into the child SKU but shorten it:
    Sizes says "2XL"/"3XL" and the children end "2X"/"3X", while M/L/XL are
    spelled out in full. Matching literally — which is all split_combination
    does — resolves three of the five and refuses the other two, on one row
    with one convention.

    ONLY WHEN THE ROW HAS ALREADY PROVED THAT SIZES ARE WRITTEN, and that is
    the whole safety argument. A trailing token is not self-evidently a size:
    in "CP-20-100 A" it is a colour code and the size is implied. Prefix
    matching a token nobody has established as a size is how "S" becomes
    "Small" on a row whose S meant Smoke Lens — a plausible mapping onto the
    wrong sizes, which is the one outcome this module exists to rule out.

    So the rule is per ROW, never per combination:

        every token matches a size exactly   -> nothing to alias
        none match                           -> the convention is unproved; {}
        some match, some do not              -> the row writes sizes, so the
                                                leftovers are abbreviations

    A row that abbreviates EVERY size ("2X"/"3X" and nothing spelled out) gets
    an empty map and fails, deliberately: with no token spelled in full there
    is nothing to distinguish it from a row whose trailing tokens are colour
    codes, and refusing is the honest answer.

    Matching is a prefix test on the raw casefolded text, NOT the _letters
    helper the colour tiers use: _letters strips non-alphabetic characters, so
    it reduces both "2X" and "3X" to "x" and both "2XL" and "3XL" to "xl" —
    discarding precisely the digit that tells the two sizes apart. An
    ambiguous hit is skipped rather than guessed, and two tokens competing for
    one size cancel each other, so the result never depends on ordering.
    """
    sizes = row.get("sizes") or []
    combinations = [c.strip() for c in (row.get("combinations") or []) if c.strip()]
    if not sizes or not combinations:
        return {}

    base = base_prefix(row)
    tokens: list[str] = []
    for combination in combinations:
        rest = combination
        if rest.upper().startswith(base.upper()):
            rest = rest[len(base):].strip()
        parts = rest.split()
        if parts and parts[-1].casefold() not in {t.casefold() for t in tokens}:
            tokens.append(parts[-1])

    wanted = {s.casefold(): s for s in sizes}
    matched = [t for t in tokens if t.casefold() in wanted]
    unmatched = [t for t in tokens if t.casefold() not in wanted]
    if not matched or not unmatched:
        return {}

    taken = {t.casefold() for t in matched}
    remaining = [s for s in sizes if s.casefold() not in taken]
    candidates = {t: [s for s in remaining
                      if s.casefold().startswith(t.casefold())] for t in unmatched}

    aliases: dict[str, str] = {}
    for token, hits in candidates.items():
        if len(hits) != 1:
            continue                        # ambiguous, and a tie is not an answer
        # Two tokens that both point at one size cancel: assigning either would
        # be a coin flip decided by iteration order.
        if sum(1 for other in candidates.values() if other == hits) != 1:
            continue
        aliases[token.casefold()] = hits[0]
    return aliases


def implied_colour(row: dict) -> str | None:
    """The colour every combination carries without writing it, or None.

    The mirror of implied_size, and it exists for the same reason. A row with
    ONE colour often does not bother encoding it — the Grainger vests spell
    their children "CP-SV4GM" + size ("CP-SV4GMM", "CP-SV4GM2X") with no
    colour code anywhere. Stripping the base and the size then leaves nothing,
    and the row failed with "has a size but no colour code" even though there
    was only ever one colour it could be.

    Refusing that was refusing arithmetic: 1 colour x 5 sizes is 5
    combinations and there are 5 child SKUs, so the pairing is forced. Same
    derivation implied_size already makes in the other direction.

    DECIDED PER ROW, and only when EVERY combination is bare. A row where some
    children carry a colour code and some do not is ambiguous — is the bare
    one a missing code, or a code that happens to look like a size? — so it
    returns None and the caller raises exactly as it did before.

    Note this is not reachable with two or more colours: the codes are what
    tell them apart, so a row that omits them is unknowable, not implied.
    """
    colours = row.get("colors") or []
    combinations = [c.strip() for c in (row.get("combinations") or []) if c.strip()]
    if len(colours) != 1 or not combinations:
        return None

    base = base_prefix(row)
    sizes = row.get("sizes") or []
    implied = implied_size(row)
    aliases = size_aliases(row)
    wanted = {s.casefold(): s for s in sizes}

    for combination in combinations:
        rest = combination
        if rest.upper().startswith(base.upper()):
            rest = rest[len(base):].strip()
        # implied is not None means no combination writes its size, so there is
        # nothing to strip and whatever is here is a colour code.
        if implied is None and sizes:
            tokens = rest.split()
            key = tokens[-1].casefold() if tokens else ""
            if key in wanted or key in aliases:
                rest = " ".join(tokens[:-1]).strip()
        if rest:
            return None                     # a code is written; nothing implied
    return colours[0]


def split_combination(combination: str, sku: str, sizes: list[str],
                      *, implied: str | None = None,
                      aliases: dict[str, str] | None = None,
                      colour_implied: bool = False) -> tuple[str, str | None]:
    """Split a combination SKU into (colour code, size) — "CP-V-200 LE 2XL" ->
    ("LE", "2XL").

    Sizes are matched literally because the payload usually writes them
    literally: the trailing token of every combination is the size name
    exactly as it appears in `sizes` ("OS", "2XL"). Whatever is left once the
    base SKU and the size are removed is the colour code.

    `aliases` (from size_aliases) covers the rows that abbreviate the size
    instead, and `colour_implied` (from implied_colour) the rows that never
    write a colour code — in which case the code comes back as "" and the
    caller maps it to the row's only colour. Both default off, so a caller
    that passes neither behaves exactly as it always did.
    """
    aliases = aliases or {}
    rest = combination.strip()
    if rest.upper().startswith(sku.upper()):
        rest = rest[len(sku):].strip()
    if not rest:
        if colour_implied and not sizes:
            return "", None                 # the child SKU *is* the parent SKU
        raise CombinationError(f"{combination!r} adds nothing to the SKU {sku!r}")

    size = None
    if implied is not None:
        # The row was classified by implied_size(): this size applies to every
        # combination and none of them write it, so there is nothing to strip.
        size = implied
    elif sizes:
        tokens = rest.split()
        wanted = {s.casefold(): s for s in sizes}
        key = tokens[-1].casefold() if tokens else ""
        if key in wanted or key in aliases:
            size = wanted.get(key) or aliases[key]
            rest = " ".join(tokens[:-1]).strip()
        else:
            raise CombinationError(
                f"{combination!r} does not end in one of the row's sizes {sizes}")
    if not rest:
        if colour_implied:
            return "", size
        raise CombinationError(f"{combination!r} has a size but no colour code")
    return rest, size


def colour_codes(row: dict) -> dict[str, str]:
    """Map each of the row's colours to its code, e.g. {"Yellow": "YW"}.

    Raises CombinationError rather than guessing when a colour and a code
    cannot be paired unambiguously.
    """
    colours = list(row.get("colors") or [])
    if not colours:
        return {}

    # One colour that no child SKU names has exactly one possible code: none.
    # Nothing to score, and no tier can improve on arithmetic.
    if implied_colour(row) is not None:
        return {colours[0]: ""}

    base = base_prefix(row)
    implied = implied_size(row)
    aliases = size_aliases(row)
    codes = []
    for combination in row.get("combinations") or []:
        code, _ = split_combination(combination, base, row.get("sizes") or [],
                                    implied=implied, aliases=aliases)
        if code not in codes:
            codes.append(code)

    if len(codes) != len(colours):
        raise CombinationError(
            f"{row['sku']}: {len(colours)} colour(s) {colours} but "
            f"{len(codes)} distinct code(s) {codes}")

    resolved: dict[str, str] = {}
    taken: set[str] = set()
    unresolved = []

    for colour in colours:
        forced = OVERRIDES.get(colour)
        if forced:
            if forced not in codes:
                raise CombinationError(
                    f"{row['sku']}: override {colour!r}->{forced!r} is not one of {codes}")
            resolved[colour] = forced
            taken.add(forced)
        else:
            unresolved.append(colour)

    for colour in unresolved:
        ranked = sorted(((score(c, colour), c) for c in codes if c not in taken),
                        reverse=True)
        if not ranked or ranked[0][0] == 0:
            raise CombinationError(
                f"{row['sku']}: no code among {codes} looks like {colour!r}. "
                f"Add it to combination_skus.OVERRIDES.")
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            raise CombinationError(
                f"{row['sku']}: {colour!r} matches {ranked[0][1]!r} and "
                f"{ranked[1][1]!r} equally well. Add it to "
                f"combination_skus.OVERRIDES.")
        resolved[colour] = ranked[0][1]
        taken.add(ranked[0][1])

    return resolved


def combination_map(row: dict, codes: dict[str, str] | None = None
                    ) -> dict[tuple[str, str | None], str]:
    """{(colour, size): combination SKU} for every combination in the row.

    `size` is None for rows with no sizes. Every combination in the payload must
    be accounted for — a leftover means the colour mapping is wrong, and it is
    better to hear that here than to write a wrong SKU into the catalogue.

    `codes` lets a caller supply a colour->code mapping from somewhere other
    than the built-in heuristic (app.colour resolves it from a curated map and,
    failing that, a model). The checks below are unchanged and still apply — an
    injected mapping earns no more trust than a derived one, which is precisely
    what makes it safe to let a model propose one.
    """
    codes = colour_codes(row) if codes is None else codes
    by_code = {code: colour for colour, code in codes.items()}
    sizes = row.get("sizes") or []
    base = base_prefix(row)
    implied = implied_size(row)
    aliases = size_aliases(row)
    colour_implied = implied_colour(row) is not None

    mapping: dict[tuple[str, str | None], str] = {}
    for combination in row.get("combinations") or []:
        code, size = split_combination(combination, base, sizes, implied=implied,
                                       aliases=aliases,
                                       colour_implied=colour_implied)
        colour = by_code.get(code)
        if colour is None:
            raise CombinationError(
                f"{row['sku']}: combination {combination!r} uses code {code!r}, "
                f"which matched no colour (mapped: {codes})")
        key = (colour, size)
        if key in mapping:
            raise CombinationError(
                f"{row['sku']}: two combinations for {key}: "
                f"{mapping[key]!r} and {combination!r}")
        mapping[key] = combination

    expected = len(row.get("colors") or []) * max(1, len(sizes))
    if len(mapping) != expected:
        raise CombinationError(
            f"{row['sku']}: {len(mapping)} combination(s) for "
            f"{len(row.get('colors') or [])} colour(s) x {max(1, len(sizes))} size(s) "
            f"— expected {expected}")
    return mapping
