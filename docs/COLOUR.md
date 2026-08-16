# Colour resolution and swatches

**Role:** How a colour name becomes a child-SKU code and (separately) a storefront swatch id.
**Not for:** Combination grid editing ([`AUTOMATION.md`](AUTOMATION.md)) or LLM observability storage ([`STORE.md`](STORE.md), [`OBSERVABILITY.md`](OBSERVABILITY.md)).

Verified against: `app/colour/resolver.py`, `store.py`, `llm.py`, `swatch.py`, `combination_skus.py`, `tests/test_colour_resolution.py`, `tests/test_colour_swatches.py` (**2026-08-17**).

Two different questions:

| Question | Module | Source of truth |
|---|---|---|
| Which short code is this colour in **this row’s** child SKUs? | `app.colour.resolve` | Closed sets extracted from the row, then verified |
| Which swatch id should the value editor post? | `app.colour.swatch` | Workbook “Color Swatch” column + live dropdown + map |

Do not collapse them. A code is derived from the payload. A swatch is a catalogue fact the payload cannot imply (e.g. “Hi-Vis Lime” filed as “DSHigh-Vis Green”).

## Colour codes — three tiers per colour

```
extract codes from combinations
  if len(codes) != len(colours) → fail (no tier runs)
tier 1  ColourMap.lookup(colour, source_sheet) if code still remaining
tier 2  greedy _heuristic (ties and zeros left for later)
tier 3  llm.infer(remaining colours, remaining codes) if OPENAI_API_KEY
elimination  one colour + one code left → take it (tier labelled heuristic)
unresolved → Resolution.error
combination_map(row, codes) → verified or error
_persist LLM hits only, verified=False
```

`resolve()` never raises; the runner turns `not resolution.ok` into `CombinationError`.

### Scoped map (not flat)

`SEED` in `app/colour/store.py` is the built-in fallback. Lookup: `(scope, key)` then `('global', key)`.

`normalise`: casefold, strip punctuation, collapse whitespace. **Vis vs Viz is kept distinct** — on this catalogue they are different codes (HLE vs LE).

Storage overlay order when loading: SEED, then `var/colour-map.json`, then Postgres `colour_codes` (later wins). Losing the DB costs entries, not the ability to resolve from SEED + heuristic.

### LLM contract (`app/colour/llm.py`)

- Shared system prompt: bijection, never invent a code, use family examples.
- User payload: `product_family`, `colours`, `available_codes`, `worked_examples` (up to 5 same-scope map entries). No SKUs, no credentials, no product body.
- `temperature=0`, `response_format` json_schema `colour_assignment` strict, timeout 10s, 2 transport retries.
- Cost estimate uses gpt-4o-mini list prices; UI running total only, not billing.

Prompt vs code: see invariants. The prompt is **PROMPT-ONLY** for matching *style*; inventing a code is blocked in `infer` and again in `combination_map`.

**Accepted behaviour:** an LLM assignment that verifies is **used immediately** and stored unverified. Human confirm (`POST /api/colours/confirm`) sets `verified=True` / `source=human`. Verification ≠ correctness (two colours could be swapped and still count).

### Heuristic vs `combination_skus.colour_codes`

`_heuristic` leaves unscorable/tied colours out (hands them to tier 3). `colour_codes` **raises** on those cases. Production runner uses `resolve`, not `colour_codes` directly.

## Swatches

Workbook column optional: empty → no swatches, no warning.

Equal-length colour/swatch lists pair **by position** (so “Sunshine” can request the “Yellow” swatch). Unequal lengths fall back to **exact name** match after `normalise` — never “take the first N”.

Tiers: map (id must still carry the recorded label on **this** admin) → unique label in the live dropdown → unresolved.

`SEED` ids/labels in `app/colour/swatch.py` were measured on a specific admin. They are instance-specific database keys. `_verify` refuses a missing id or a label mismatch rather than painting the wrong swatch.

Unresolved swatches **must not fail the product**. The runner logs and records an event.

## Current payload facts (re-verify; not laws)

Checked-in fixture: `parsed_rows.json`.

```bash
python3 - <<'PY'
import json
from collections import Counter
from pathlib import Path
data = json.loads(Path("parsed_rows.json").read_text())
rows = data["rows"] if isinstance(data, dict) else data
print("rows", len(rows), "sheets", Counter(r.get("source_sheet") for r in rows))
print("skus", [r["sku"] for r in rows])
colours = sorted({c for r in rows for c in (r.get("colors") or [])})
print("distinct colours", len(colours), colours)
PY
```

Colour-map seed size:

```bash
python3 - <<'PY'
from app.colour.store import SEED
print(sum(len(v) for v in SEED.values()), "seed entries", list(SEED))
PY
```

Whether tier 3 is needed on that fixture is pinned by `tests/test_colour_resolution.py` `test_the_payload_needs_no_model_at_all` (offline). If that test fails, the map/heuristic drifted — curate the map; do not “fix” by turning the model on in tests.

## Non-goals

- Auto-promoting LLM entries to `verified=True`
- Caching heuristic results into the shared map (`_persist` explains why that is wrong)
- String-similarity swatch picking
- Treating swatch counts on a given admin (~thousands of dropdown options) as a frozen law
