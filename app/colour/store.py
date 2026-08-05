"""Tier 1 — the curated colour map.

    Yellow (HardHat) -> YW

Scoped, not flat. That is the whole point of this module, and it is not
premature generality — the shipped payload already contains a direct
counterexample to a flat map:

    HardHat   Hi-Vis Lime -> HLE      Hi-Vis Orange -> HOE
    Vest      Hi-Viz Lime -> LE       Hi-Viz Orange -> OE
    Vest      Lime        -> LE       Orange        -> OE

The same colour carries a different code depending on the product family. A
flat {colour: code} map survives today only because "Vis" and "Viz" happen to
be spelled differently, which nobody promised would hold. Write "Hi-Vis Lime"
on a vest sheet and a flat map hands back HLE — a SKU belonging to no product.

Lookup order is (scope, colour) then ('global', colour). Scope comes from the
row's source_sheet, which the payload already carries.

STORAGE: Supabase when configured, a local JSON file otherwise, and the
built-in SEED when neither has an answer. All three are the same shape, and the
automation runs identically against any of them — losing the database costs
observability, never correctness.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from app.config import ROOT, settings

log = logging.getLogger("colour.store")

GLOBAL = "global"
LOCAL_PATH = ROOT / "var" / "colour-map.json"


def normalise(colour: str) -> str:
    """The map key for a colour name.

    Casefold, collapse whitespace, drop punctuation — so "Hi-Vis Lime",
    "hi vis lime" and "Hi-Vis  Lime" are one entry.

    Vis/Viz is deliberately NOT normalised away. On the current data they are
    genuinely different entries pointing at different codes (HLE vs LE), and
    folding them together would silently pick one code for both. If a client
    ever confirms the two spellings mean the same thing in the same scope, that
    belongs in the map as two rows, not in this function as a rule.
    """
    text = re.sub(r"[^a-z0-9\s]", " ", colour.casefold())
    return re.sub(r"\s+", " ", text).strip()


#: Derived from the shipped payload by the tier-2 heuristic, then read back and
#: confirmed against the combination SKUs. Present so a fresh install resolves
#: the known catalogue with no database and no API key.
SEED: dict[str, dict[str, str]] = {
    "HardHat": {
        "yellow": "YW", "white": "WE", "blue": "BE", "green": "GN",
        "pink": "PK", "red": "RD", "grey": "GY",
        "hi vis orange": "HOE", "hi vis lime": "HLE",
    },
    "Vest": {
        "hi viz lime": "LE", "hi viz orange": "OE",
        "lime": "LE", "orange": "OE",
    },
}


@dataclass(frozen=True)
class Entry:
    scope: str
    colour_raw: str
    colour_key: str
    code: str
    source: str          # seed | heuristic | llm | human
    verified: bool


class ColourMap:
    """The map, cached in memory for the life of the process.

    Reads are hot — every row hits this once per colour — and the underlying
    data changes on human timescales, so a process-lifetime cache with an
    explicit refresh() is the right trade. A long-lived worker should refresh
    between runs, not between rows.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], Entry] | None = None

    # --- loading ---------------------------------------------------------

    def _load(self) -> dict[tuple[str, str], Entry]:
        entries: dict[tuple[str, str], Entry] = {}

        for scope, pairs in SEED.items():
            for key, code in pairs.items():
                entries[(scope, key)] = Entry(scope, key, key, code, "seed", True)

        for entry in self._load_local() + self._load_remote():
            entries[(entry.scope, entry.colour_key)] = entry

        return entries

    def _load_local(self) -> list[Entry]:
        if not LOCAL_PATH.exists():
            return []
        try:
            raw = json.loads(LOCAL_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("colour map %s unreadable: %s", LOCAL_PATH, exc)
            return []
        return [Entry(e["scope"], e.get("colour_raw", e["colour_key"]),
                      e["colour_key"], e["code"], e.get("source", "human"),
                      bool(e.get("verified", False)))
                for e in raw]

    def _load_remote(self) -> list[Entry]:
        if not settings().store_enabled:
            return []
        try:
            from app.store.client import get_client
            rows = get_client().table("colour_codes").select("*").execute().data
        except Exception as exc:                # never let the DB stop a run
            log.warning("could not load colour map from Supabase: %s", exc)
            return []
        return [Entry(r["scope"], r.get("colour_raw", ""), r["colour_key"],
                      r["code"], r.get("source", "human"),
                      bool(r.get("verified", False))) for r in rows]

    @property
    def entries(self) -> dict[tuple[str, str], Entry]:
        with self._lock:
            if self._entries is None:
                self._entries = self._load()
                log.info("colour map: %d entries", len(self._entries))
            return self._entries

    def refresh(self) -> None:
        with self._lock:
            self._entries = None

    # --- lookup ----------------------------------------------------------

    def lookup(self, colour: str, scope: str | None) -> Entry | None:
        """Scoped first, then global. None when neither knows this colour."""
        key = normalise(colour)
        entries = self.entries
        for candidate in ([scope] if scope else []) + [GLOBAL]:
            entry = entries.get((candidate, key))
            if entry:
                return entry
        return None

    # --- writing ---------------------------------------------------------

    def record(self, scope: str, colour_raw: str, code: str, *,
               source: str, verified: bool = False,
               confidence: float | None = None) -> None:
        """Persist a resolution so the next run does not have to derive it.

        Never overwrites a verified entry with an unverified one: a human
        decision outranks anything the heuristic or the model proposes later.
        """
        key = normalise(colour_raw)
        existing = self.entries.get((scope, key))
        if existing and existing.verified and not verified:
            return

        entry = Entry(scope, colour_raw, key, code, source, verified)
        with self._lock:
            if self._entries is not None:
                self._entries[(scope, key)] = entry

        self._write_local()
        self._write_remote(entry, confidence)

    def _write_local(self) -> None:
        """Mirror non-seed entries to disk so a run without Supabase still
        accumulates what it learned."""
        try:
            LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = [
                {"scope": e.scope, "colour_raw": e.colour_raw,
                 "colour_key": e.colour_key, "code": e.code,
                 "source": e.source, "verified": e.verified}
                for e in self.entries.values() if e.source != "seed"
            ]
            LOCAL_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("could not write %s: %s", LOCAL_PATH, exc)

    def _write_remote(self, entry: Entry, confidence: float | None) -> None:
        if not settings().store_enabled:
            return
        try:
            from app.store.client import get_client
            get_client().table("colour_codes").upsert({
                "scope": entry.scope,
                "colour_raw": entry.colour_raw,
                "colour_key": entry.colour_key,
                "code": entry.code,
                "source": entry.source,
                "verified": entry.verified,
                "confidence": confidence,
            }, on_conflict="scope,colour_key").execute()
        except Exception as exc:
            log.warning("could not persist colour %s/%s: %s",
                        entry.scope, entry.colour_key, exc)


#: One map per process.
MAP = ColourMap()
