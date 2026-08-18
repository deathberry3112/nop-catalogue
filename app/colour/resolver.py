"""The three-tier colour resolution pipeline.

    map        an exact (scope, colour) hit in the curated table   — free, trusted
    heuristic  the shape rule in combination_skus.score            — free
    llm        OpenAI, over whatever is left                       — ~500ms, proposal

Tiers are applied PER COLOUR, not per row. A row with eight known colours and
one unknown makes one model call about one colour, with the eight consumed
codes removed from the candidate set — which usually leaves exactly one option
and turns inference into arithmetic.

Whatever comes out is verified by combination_skus.combination_map() before it
is used: every payload combination consumed exactly once, count equal to
colours x sizes. A tier can be wrong; it cannot be wrong *silently*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

import combination_skus
from app.colour import llm as llm_tier
from app.colour.store import MAP

log = logging.getLogger("colour.resolver")


class Tier(str, Enum):
    MAP = "map"
    HEURISTIC = "heuristic"
    LLM = "llm"
    UNRESOLVED = "unresolved"


@dataclass
class Resolution:
    """What each colour resolved to, and how it got there."""

    codes: dict[str, str] = field(default_factory=dict)
    tiers: dict[str, Tier] = field(default_factory=dict)
    scope: str = ""
    llm_result: llm_tier.LLMResult | None = None
    verified: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.verified

    @property
    def tier_counts(self) -> dict[str, int]:
        """The metric that matters: how much work each tier is doing. A rising
        llm share is the signal that the curated map needs attention."""
        counts: dict[str, int] = {}
        for tier in self.tiers.values():
            counts[tier.value] = counts.get(tier.value, 0) + 1
        return counts

    @property
    def used_llm(self) -> bool:
        return Tier.LLM in self.tiers.values()


def _extract_codes(row: dict) -> list[str]:
    """The distinct colour codes present in this row's combination SKUs, in
    payload order. This is why the model never has to invent one."""
    codes: list[str] = []
    base = combination_skus.base_prefix(row)
    implied = combination_skus.implied_size(row)
    aliases = combination_skus.size_aliases(row)
    for combination in row.get("combinations") or []:
        code, _ = combination_skus.split_combination(
            combination, base, row.get("sizes") or [], implied=implied,
            aliases=aliases)
        if code not in codes:
            codes.append(code)
    return codes


def _heuristic(colours: list[str], codes: list[str]) -> dict[str, str]:
    """Greedy best-score assignment over what is left.

    Unlike combination_skus.colour_codes this does not raise on an ambiguous or
    unscorable colour — it leaves it out, which is exactly what hands it to the
    next tier instead of failing the row.
    """
    resolved: dict[str, str] = {}
    available = list(codes)

    # Strongest matches first, so a colour with an unambiguous code claims it
    # before a weaker candidate can take it by being earlier in the list.
    ranked = sorted(
        ((max((combination_skus.score(c, colour) for c in available), default=0),
          colour) for colour in colours),
        reverse=True)

    for _, colour in ranked:
        scores = sorted(((combination_skus.score(c, colour), c) for c in available),
                        reverse=True)
        if not scores or scores[0][0] == 0:
            continue                                    # nothing plausible
        if len(scores) > 1 and scores[0][0] == scores[1][0]:
            continue                                    # a tie is not an answer
        resolved[colour] = scores[0][1]
        available.remove(scores[0][1])
    return resolved


def resolve(row: dict, *, allow_llm: bool = True) -> Resolution:
    """Resolve every colour in `row` to its code, then verify the whole thing.

    Never raises: a failure comes back as `Resolution.error` so the caller can
    record it against the row and carry on with the rest of the batch.
    """
    scope = row.get("source_sheet") or ""
    colours = list(row.get("colors") or [])
    resolution = Resolution(scope=scope)

    if not colours:
        resolution.verified = True                      # nothing to resolve
        return resolution

    # A single colour that no child SKU names is settled before any tier runs.
    # There is no code to match it to, so the map cannot hit, the heuristic has
    # nothing to score, and asking the model which of zero codes belongs to
    # "Lime" would spend 500ms to be told what arithmetic already knows.
    # Still verified below like every other answer.
    if combination_skus.implied_colour(row) is not None:
        resolution.codes = {colours[0]: ""}
        resolution.tiers = {colours[0]: Tier.HEURISTIC}
        try:
            combination_skus.combination_map(row, resolution.codes)
            resolution.verified = True
        except combination_skus.CombinationError as exc:
            resolution.error = f"resolved colours did not verify: {exc}"
        return resolution

    try:
        codes = _extract_codes(row)
    except combination_skus.CombinationError as exc:
        resolution.error = str(exc)
        return resolution

    if len(codes) != len(colours):
        resolution.error = (
            f"{row['sku']}: {len(colours)} colour(s) {colours} but "
            f"{len(codes)} distinct code(s) {codes}")
        return resolution

    remaining_colours = list(colours)
    remaining_codes = list(codes)

    # --- tier 1: the curated map ----------------------------------------
    for colour in list(remaining_colours):
        entry = MAP.lookup(colour, scope)
        # A mapped code that this row does not actually contain is a stale map
        # entry, not an answer. Fall through and let a later tier decide.
        if entry and entry.code in remaining_codes:
            resolution.codes[colour] = entry.code
            resolution.tiers[colour] = Tier.MAP
            remaining_colours.remove(colour)
            remaining_codes.remove(entry.code)

    # --- tier 2: the shape heuristic ------------------------------------
    if remaining_colours:
        for colour, code in _heuristic(remaining_colours, remaining_codes).items():
            resolution.codes[colour] = code
            resolution.tiers[colour] = Tier.HEURISTIC
            remaining_colours.remove(colour)
            remaining_codes.remove(code)

    # --- tier 3: the model ----------------------------------------------
    if remaining_colours and allow_llm and llm_tier.available():
        examples = [(e.colour_raw or e.colour_key, e.code)
                    for (entry_scope, _), e in MAP.entries.items()
                    if entry_scope == scope][:5]
        result = llm_tier.infer(remaining_colours, remaining_codes,
                                scope=scope, examples=examples)
        resolution.llm_result = result
        if result.ok:
            for colour, code in result.assignments.items():
                if code in remaining_codes:
                    resolution.codes[colour] = code
                    resolution.tiers[colour] = Tier.LLM
                    remaining_colours.remove(colour)
                    remaining_codes.remove(code)

    # A single unresolved colour with a single unused code has exactly one
    # possible answer. Taking it is deduction, not guessing.
    if len(remaining_colours) == 1 and len(remaining_codes) == 1:
        colour = remaining_colours.pop()
        resolution.codes[colour] = remaining_codes.pop()
        resolution.tiers[colour] = Tier.HEURISTIC
        log.info("resolved %r by elimination", colour)

    for colour in remaining_colours:
        resolution.tiers[colour] = Tier.UNRESOLVED

    if remaining_colours:
        resolution.error = (
            f"{row['sku']}: could not resolve {remaining_colours} against "
            f"codes {remaining_codes}. Add them to the colour map.")
        return resolution

    # --- verification: the gate every tier answers to --------------------
    try:
        combination_skus.combination_map(row, resolution.codes)
        resolution.verified = True
    except combination_skus.CombinationError as exc:
        resolution.error = f"resolved colours did not verify: {exc}"
        return resolution

    _persist(resolution)
    return resolution


def _persist(resolution: Resolution) -> None:
    """Record what only the model could work out, so the next run gets a tier-1
    hit and a human gets something to confirm.

    ONLY tier 3 is persisted, deliberately. Caching heuristic results would be
    the obvious thing to do and it is wrong twice over: the heuristic is
    deterministic and free, so a cache buys nothing, and writing its guesses
    into the shared map turns one row's inference into an authoritative-looking
    fact for every later row — while burying the entries that genuinely need
    review under dozens that do not. A review queue nobody can face is a review
    queue nobody uses.

    Recorded unverified: passing verification proves the assignment is
    self-consistent with this payload, which is strong but not the same as
    correct — two colours with equally plausible codes could be swapped and
    still satisfy every count. Human confirmation closes that gap.
    """
    if resolution.llm_result is None:
        return
    for colour, code in resolution.codes.items():
        if resolution.tiers.get(colour) is Tier.LLM:
            MAP.record(resolution.scope or "global", colour, code,
                       source=Tier.LLM.value, verified=False,
                       confidence=resolution.llm_result.confidence.get(colour))
