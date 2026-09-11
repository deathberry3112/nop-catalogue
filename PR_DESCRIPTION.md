<!--
PR description for feature/implied-colour-and-size-aliases -> main
Open at: https://github.com/pranayupbott/nop-commerce-catalogue-automation/pull/new/feature/implied-colour-and-size-aliases

Title: Read the rows that abbreviate a size or omit the colour

This file is untracked scratch — delete it once the PR is open.
-->

The Grainger `CP-SV4GM` row failed twice over: its child SKUs cut `2XL`/`3XL`
to `2X`/`3X`, and they carry no colour code at all. Both are now read, and both
are read conservatively — every new inference is derived per row, or refused.

## Sizes the child SKUs abbreviate

`size_aliases()` maps the short tokens, but only where the row has already
proved it writes sizes: some spelled out in full, some not. All match — nothing
to alias. None match — the convention is unproved, so `{}`. A row that
abbreviates *every* size still fails, deliberately: with nothing spelled out,
its trailing tokens are indistinguishable from colour codes, and `S` becoming
`Small` on a row where S meant Smoke Lens is the one outcome this module exists
to rule out.

Matching is a raw casefolded prefix test rather than `_letters()`, which strips
the digit separating `2X` from `3X`. Ambiguous hits are skipped and two tokens
competing for one size cancel, so no result depends on iteration order.

## A single colour nothing writes a code for

`implied_colour()` mirrors `implied_size()`. One colour over five sizes and five
children forces the pairing, so refusing it was refusing arithmetic. It fires
only where **every** combination is bare — a row that names its colour on some
children and not others stays ambiguous and raises exactly as before. Not
reachable with two or more colours: the codes are what tell them apart.

The resolver settles that case before any tier runs. There is no code to match,
so the map cannot hit, the heuristic has nothing to score, and asking the model
which of zero codes is "Lime" would spend 500ms to be told what arithmetic
already knows. Still verified through `combination_map` like every other answer.

`split_combination` gained `aliases=` and `colour_implied=`, both defaulting
off — callers that pass neither behave exactly as they always did.

## Verified

- Full suite: **243 passed, 11 skipped**
- 11 new cases: the `CP-SV4GM` row end to end, plus every refusal — unproved
  convention, colour-code-as-size, ambiguous prefix, cancelling tokens, two
  colours without codes, half-written codes
- One test asserts the `_letters()` trap directly, so reusing it here would fail

🤖 Generated with [Claude Code](https://claude.com/claude-code)
