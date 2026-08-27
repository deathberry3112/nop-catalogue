"""Which colour swatch the workbook asked for.

    swatches, problems, outcomes = swatches_for_row(row, options)
    swatches            -> {"Yellow": "25", "Hi-Vis Lime": "2071"}
    outcomes            -> one entry per colour the sheet asked about, for the
                           console: what it got, or why it got nothing

THE SHEET DECIDES, and that is the first thing to know about this module. The
template carries a "Color Swatch" column beside "Color"; app.parser splits it
into row["swatches"], and swatches_for_row() below pairs the two by position.
An empty column means no swatches, silently — filling that column is optional
and leaving it blank is a legitimate way to fill the sheet, not an omission to
be repaired by inference.

What is left for this module is narrow but not trivial: turning the name the
sheet writes into an id this admin will accept.

    choices = resolve_swatches(["Hi-Vis Lime"], options, scope="HardHat")
    choices["Hi-Vis Lime"].swatch_id   -> "2071"   ("DSHigh-Vis Green")

`options` is the live contents of the value editor's swatch dropdown, read off
the page — this module never invents an id, it only chooses among the ones the
admin is offering.

WHY THAT NEEDS A MAP AND NOT A STRING MATCH, which is not obvious until you
look at the data. This admin offers 2,114 swatches, and matching the name the
sheet writes against them fails in both directions at once:

    Yellow, White, Blue,        TWO swatches carry each of these labels, with
    Green, Red, Lime, Orange    nothing in the DOM to tell them apart — no
                                optgroup, no title, no data attribute
    Hi-Vis Lime                 no swatch is called that; the client's own hard
                                hat uses "DSHigh-Vis Green" (id 2071)
    Pink, Grey                  exactly one swatch each, and those are safe

Tier 2 settles both of the first two lines by the name alone: a swatch whose
label IS the colour is the answer, and when several carry that same label the
first one the dropdown offers wins. Duplicates are the same swatch entered
twice as far as the page is concerned, so refusing to choose between them
bought nothing but an unset swatch. What tier 2 will not do is match on
anything short of the whole name — a near-miss picked by string similarity
would be a plausible swatch on the wrong product, silently, which is the
outcome this codebase is built to rule out (see app/colour/store.py and
combination_skus).

The third line is the case a name cannot settle at all: the client decided
that Hi-Vis Lime is filed under a swatch named Green, and a business fact like
that comes from the map or the colour keeps no swatch. The map also outranks
tier 2 wherever the first duplicate is the wrong one — that is what the
`global` block below is measured for.

WHAT TIER 2 CANNOT SEE, and it is worth knowing before trusting it: the same
colour name legitimately takes a different swatch on a different product
family. Across the client's own catalogue "Black" is filed as "Black" (40),
"DSBlack" (2068) and "Propper Black" (2124) depending on the brand — 34 colour
names behave this way. Tier 2 will pick the plainly-named one every time, which
is right for the workbook families this automation runs and wrong for a branded
line. When one of those arrives, the fix is a scoped SEED entry, not a cleverer
rule — that is what `scope` is for.

SEED evidence: read off the client's own hand-configured products on the live
instance, not guessed. Add to it by editing SEED, var/colour-swatches.json, or
the colour_swatches table — all three have the same shape.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from enum import Enum

from app.colour.store import GLOBAL, normalise
from app.config import ROOT, settings

log = logging.getLogger("colour.swatch")

LOCAL_PATH = ROOT / "var" / "colour-swatches.json"


class Tier(str, Enum):
    MAP = "map"                 # the curated table said so
    LABEL = "label"             # a swatch carries this name (the first, if several)
    UNRESOLVED = "unresolved"   # nothing confident — leave the swatch unset


#: (scope, colour) -> (swatch id, the label that id MUST still carry).
#:
#: The label is not decoration. Swatch ids are per-instance database keys, so a
#: seed taken from one admin is a guess about any other — recording what the id
#: was called lets _verify below refuse an id that has come to mean something
#: else, instead of quietly filing a product under whatever swatch 25 is on
#: this server. An id/label disagreement is a stale map entry, not an answer.
#:
#: MEASURED, NOT CHOSEN. Every entry below is what the client's own
#: hand-configured products actually use, counted across the 204 attribute
#: mappings on zinggreenproductsdev that have a swatch set. The `global` block
#: is what keeps the duplicate labels honest — resolve_swatches would otherwise
#: take whichever of the two the dropdown lists first, and the catalogue is
#: emphatic about which one is real:
#:
#:     lime    23 x113   37 never        orange  24 x139   38 x2
#:     blue    19 x29    33 never        yellow  25 x40    39 never
#:     red     20 x29    34 never        green   22 x23    36 never
#:     white   18 x40  2049 x1
#:
#: The scoped blocks are for colours whose swatch cannot be derived from the
#: name at all: a hard hat's "Hi-Vis Lime" is filed under a swatch called
#: "DSHigh-Vis Green" (mapping 718).
SEED: dict[str, dict[str, tuple[str, str]]] = {
    GLOBAL: {
        "yellow": ("25", "Yellow"),
        "white":  ("18", "White"),
        "blue":   ("19", "Blue"),
        "green":  ("22", "Green"),
        "red":    ("20", "Red"),
        "lime":   ("23", "Lime"),
        "orange": ("24", "Orange"),
        "black":  ("40", "Black"),
        "grey":   ("2051", "Grey"),
        "silver": ("2064", "Silver"),
    },
    "HardHat": {
        "hi vis lime":   ("2071", "DSHigh-Vis Green"),
        "hi vis orange": ("2072", "DSHigh-Vis Orange"),
    },
}


@dataclass(frozen=True)
class Swatch:
    scope: str
    colour_raw: str
    colour_key: str
    swatch_id: str
    swatch_label: str
    source: str          # seed | human
    verified: bool


@dataclass(frozen=True)
class Choice:
    """What one colour resolved to, and why.

    `reason` is prose for a human reading one product. `cause` is the same
    thing as a slug, because the console groups a 200-row run by it and prose
    does not group — see CAUSE below for the vocabulary. `alternatives` carries
    the ids this choice beat when several swatches share the label, which is
    the one case a resolved colour is still worth showing somebody.
    """

    colour: str
    tier: Tier
    swatch_id: str | None = None
    swatch_label: str | None = None
    reason: str = ""
    cause: str = ""
    alternatives: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.swatch_id is not None


#: Why a colour ended up where it did. The console groups on these, so they are
#: part of the interface: renaming one silently re-labels a group in the UI.
CAUSE = {
    "duplicate":      "took the first of several identically-named swatches",
    "unnamed":        "no swatch carries this name",
    "map_missing_id": "the swatch map points at an id this admin does not offer",
    "map_stale":      "the swatch map is stale — that id means something else here",
    "unpaired":       "the sheet's swatch column could not be paired with the colours",
}


class SwatchMap:
    """The curated colour -> swatch table, cached for the life of the process.

    Same shape and same trade as ColourMap in app/colour/store.py: SEED, plus a
    local JSON mirror, plus Postgres when it is configured. Losing the database
    costs entries, never correctness — an entry that is not there resolves by
    label or not at all.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], Swatch] | None = None

    def _load(self) -> dict[tuple[str, str], Swatch]:
        entries: dict[tuple[str, str], Swatch] = {}
        for scope, pairs in SEED.items():
            for key, (swatch_id, label) in pairs.items():
                entries[(scope, key)] = Swatch(scope, key, key, swatch_id, label,
                                               "seed", True)
        for entry in self._load_local() + self._load_remote():
            entries[(entry.scope, entry.colour_key)] = entry
        return entries

    def _load_local(self) -> list[Swatch]:
        if not LOCAL_PATH.exists():
            return []
        try:
            raw = json.loads(LOCAL_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("swatch map %s unreadable: %s", LOCAL_PATH, exc)
            return []
        return [self._entry(e) for e in raw]

    def _load_remote(self) -> list[Swatch]:
        if not settings().store_enabled:
            return []
        try:
            from app.store.client import fetch_all
            rows = fetch_all("SELECT * FROM colour_swatches")
        except Exception as exc:            # never let the DB stop a run
            log.warning("could not load the swatch map from Postgres: %s", exc)
            return []
        return [self._entry(r) for r in rows]

    @staticmethod
    def _entry(raw: dict) -> Swatch:
        colour_raw = raw.get("colour_raw") or raw.get("colour_key", "")
        return Swatch(raw.get("scope") or GLOBAL, colour_raw,
                      raw.get("colour_key") or normalise(colour_raw),
                      str(raw["swatch_id"]), raw.get("swatch_label", ""),
                      raw.get("source", "human"), bool(raw.get("verified", False)))

    @property
    def entries(self) -> dict[tuple[str, str], Swatch]:
        with self._lock:
            if self._entries is None:
                self._entries = self._load()
                log.info("swatch map: %d entries", len(self._entries))
            return self._entries

    def refresh(self) -> None:
        with self._lock:
            self._entries = None

    def lookup(self, colour: str, scope: str | None) -> Swatch | None:
        """Scoped first, then global — the same order as the colour-code map,
        and for the same reason: a colour can mean a different swatch on a
        different product family."""
        key = normalise(colour)
        entries = self.entries
        for candidate in ([scope] if scope else []) + [GLOBAL]:
            entry = entries.get((candidate, key))
            if entry:
                return entry
        return None


#: One map per process.
MAP = SwatchMap()


def _by_label(options: list[tuple[str, str]]) -> dict[str, list[tuple[str, str]]]:
    grouped: dict[str, list[tuple[str, str]]] = {}
    for swatch_id, label in options:
        grouped.setdefault(normalise(label), []).append((swatch_id, label))
    return grouped


def resolve_swatches(colours: list[str], options: list[tuple[str, str]], *,
                     scope: str | None = None) -> dict[str, Choice]:
    """Choose a swatch for each colour, or say plainly that there isn't one.

    `options` is [(swatch id, label)] exactly as the dropdown lists it. Never
    raises: an unresolved colour comes back as a Choice with tier UNRESOLVED and
    a reason worth printing, because a missing swatch must not fail a product
    that is otherwise correct.
    """
    known = {str(swatch_id): label for swatch_id, label in options}
    grouped = _by_label(options)
    out: dict[str, Choice] = {}

    for colour in colours:
        entry = MAP.lookup(colour, scope)
        if entry:
            out[colour] = _verify(colour, entry, known)
            continue

        hits = grouped.get(normalise(colour), [])
        if hits:
            swatch_id, label = hits[0]
            others = tuple(i for i, _ in hits[1:])
            reason = (
                f"the only swatch named {label!r} in this store" if len(hits) == 1
                else f"{len(hits)} swatches are named {label!r} and nothing tells "
                     f"them apart, so this run used the first "
                     f"(ids {', '.join(i for i, _ in hits)}) — save the colour to "
                     f"your swatch list if you meant the other one")
            out[colour] = Choice(colour, Tier.LABEL, swatch_id, label, reason,
                                 cause="duplicate" if others else "",
                                 alternatives=others)
        else:
            out[colour] = Choice(
                colour, Tier.UNRESOLVED, cause="unnamed",
                reason=f"no swatch in this store is named {colour!r} — save which "
                       f"one it should use")
    return out


def _outcome(colour: str, asked: str, choice: Choice | None, *,
             cause: str = "", note: str = "") -> dict:
    """One colour's story, as JSON the console can render.

    A dict rather than a dataclass on purpose: this goes straight into
    row_steps.detail and comes back out of Postgres, so its shape IS the wire
    format. `state` is filled in later by the runner, which is the only layer
    that knows what the admin accepted; until then it says what was intended.
    """
    if choice is None:
        return {"colour": colour, "asked": asked, "swatch_id": None,
                "swatch_label": None, "tier": Tier.UNRESOLVED.value,
                "cause": cause, "alternatives": [], "note": note,
                "state": "skipped"}
    return {"colour": colour, "asked": asked, "swatch_id": choice.swatch_id,
            "swatch_label": choice.swatch_label, "tier": choice.tier.value,
            "cause": choice.cause, "alternatives": list(choice.alternatives),
            "note": choice.reason,
            "state": "planned" if choice.ok else "skipped"}


def swatches_for_row(row: dict, options: list[tuple[str, str]]
                     ) -> tuple[dict[str, str], list[str], list[dict]]:
    """({colour: swatch id}, [reasons], [outcome]) for one payload row.

    The third element is the whole story rather than only the bad half: one
    entry per colour the sheet asked a swatch for, resolved or not, in the
    sheet's own order. It exists because "which swatches were done" is a
    question the console could not answer from the first two — a run that set
    nothing and a run that set everything both reported silence.

    Colours the sheet did NOT ask about are absent from it, deliberately. A
    blank cell is a decision, and a UI that printed a line per unasked colour
    would bury the four that need a person under four hundred that do not.

    THE WORKBOOK DECIDES. The sheet carries a "Color Swatch" column beside
    "Color", and app.parser already splits it into row["swatches"]. That column
    is the input to this whole module — not the colour name, and not a rule
    about colour names. An empty column means the person who filled the sheet
    did not ask for swatches, so none are set and nothing is reported: that is
    a legitimate way to fill the sheet, not a problem to warn about.

    The map below still matters, but only to translate what the column says
    into an id: the sheet writes "Hi-Vis Lime" and this admin calls that swatch
    "DSHigh-Vis Green", and two different swatches are both called "Blue".

    WHEN THE COUNTS DISAGREE, position stops meaning anything — but that is not
    the same as knowing nothing. CP-30-F100V lists 5 colours against 9 swatches
    (the column is the sibling row's longer list, pasted), and its 5 colours
    appear verbatim among those 9. Matching those by NAME is not a guess and
    not a similarity score: an entry spelled exactly like the colour pairs with
    it whatever order either list is in. What taking the first five would have
    done — assume the extra entries are at the end — is the guess, and it is
    the one thing this avoids.

    So: equal lengths pair by position, because that is the sheet's own
    intent and it is what lets a colour carry a differently-named swatch
    ("Sunshine" wanting the "Yellow" swatch). Unequal lengths fall back to
    matching on name, and any colour with no same-named entry gets nothing and
    is named in the report. app.parser warns about the mismatch separately.
    """
    return swatches_for_values(
        list(row.get("colors") or []), list(row.get("swatches") or []),
        options, scope=row.get("source_sheet"))


def swatches_for_values(values: list[str], wanted: list[str],
                        options: list[tuple[str, str]], *,
                        scope: str | None = None
                        ) -> tuple[dict[str, str], list[str], list[dict]]:
    """The same question for ONE attribute's values, whatever it is called.

    swatches_for_row is the colour-shaped caller; this is what the runner uses
    once attributes stopped being colour and size. A swatch is asked for by a
    "<Attribute> Swatch" column beside the values, so any attribute may have
    one — the map and the label matching below do not care what the attribute
    is named, only what the admin is offering.
    """
    colours = list(values)
    wanted = list(wanted)
    if not colours or not any(w.strip() for w in wanted):
        return {}, [], []

    mismatch = len(wanted) != len(colours)
    if mismatch:
        by_name = {normalise(w): w for w in wanted if w.strip()}
        pairs = [(c, by_name.get(normalise(c))) for c in colours]
    else:
        pairs = list(zip(colours, wanted))

    names = [name for _, name in pairs if name and name.strip()]
    choices = resolve_swatches(names, options, scope=scope)

    resolved: dict[str, str] = {}
    problems: list[str] = []
    outcomes: list[dict] = []
    for colour, name in pairs:
        if not name or not name.strip():
            if mismatch:
                why = (f"the sheet lists {len(wanted)} swatch(es) for "
                       f"{len(colours)} colour(s), so they cannot be matched up "
                       f"in order, and none of them is named {colour!r}")
                problems.append(f"{colour} — {why}")
                outcomes.append(_outcome(colour, "", None, cause="unpaired",
                                         note=why))
            continue
        choice = choices[name]
        outcomes.append(_outcome(colour, name, choice))
        if choice.ok:
            resolved[colour] = choice.swatch_id
        else:
            problems.append(f"{colour} (sheet says {name!r}) — {choice.reason}")
    return resolved, problems, outcomes


def _verify(colour: str, entry: Swatch, known: dict[str, str]) -> Choice:
    """A map entry is a claim about this admin; check it before acting on it."""
    label = known.get(entry.swatch_id)
    if label is None:
        return Choice(colour, Tier.UNRESOLVED, cause="map_missing_id",
                      reason=f"your swatch list sends {colour!r} to swatch "
                             f"{entry.swatch_id}, which this store does not offer")
    if entry.swatch_label and normalise(label) != normalise(entry.swatch_label):
        return Choice(colour, Tier.UNRESOLVED, cause="map_stale",
                      reason=f"your swatch list sends {colour!r} to swatch "
                             f"{entry.swatch_id} as {entry.swatch_label!r}, but in "
                             f"this store swatch {entry.swatch_id} is {label!r} — "
                             f"the saved entry is out of date")
    # Named for the person reading a run, not for the person who wrote the map:
    # "global" is the scope's internal name and means nothing to a reader, so
    # the note says what the scope DOES instead of what it is called.
    where = ("saved for every product" if entry.scope == GLOBAL
             else f"saved for {entry.scope} products only")
    return Choice(colour, Tier.MAP, entry.swatch_id, label,
                  f"your swatch list — {where}")