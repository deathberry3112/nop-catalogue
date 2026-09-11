# Documentation index

**Role:** Where to look, who is allowed to be right, and which file owns which job.
**Not for:** Mechanism walkthroughs, the full invariant tables, or how to run a change.

Verified against: live modules under `app/`, `main.py`, `pages/`, `tests/`, `Dockerfile` (**2026-08-17**).

## Authority order (never invert)

1. **Live application code** — the only source of truth for behaviour.
2. **`/docs`** — must match code. If they disagree, trust code and fix the docs in the same change set.
3. **Root `README.md`** — implementer pointer and as-implemented stack.

There is no fourth layer. Git history and unused code comments are not specs.

Cursor rules tell an agent **how to think and when to stop**. They do not replace (2).

## Three layers (do not merge)

| Layer | Owns | Lives in | Must NOT contain |
|---|---|---|---|
| **Process / how to think** | Change loop, hard safety gate, visible change brief, verify, docs-sync | `.cursor/rules/nop-catalogue-change-process.mdc` (`alwaysApply: true`) + [`CHANGE_PROCESS.md`](CHANGE_PROCESS.md) | Deep mechanism walkthroughs, frozen inventory counts |
| **Laws / what must remain true** | Product + architecture invariants with a Proof column pointing at live modules | [`ARCHITECTURE_INVARIANTS.md`](ARCHITECTURE_INVARIANTS.md) | Tutorials, “how to run locally” (that is [`LOCAL.md`](LOCAL.md)) |
| **Mechanisms / how it works** | Stack, run path, domain behaviour, contracts, ops | the other files below | Process sermons; a second copy of the full law table |

- Cursor rule = **how an agent must think and when it must stop**
- Invariants file = **what must remain true**, with code proof
- Other docs = **how the system works** (field names, paths, current counts, runbooks)

A narrative that disagrees with the invariants file or live code is wrong.

## Invariant summary (not the tables)

This automation writes to a **production nopCommerce catalogue**. A wrong SKU, a silent column drop, or a delete of someone else’s product is expensive. The laws that follow from that:

- Refuse a wrong catalogue with a clean report (unknown columns, unverified colour maps, guessed swatches).
- Preview does not save. Commit is the only create. Rollback deletes what **this run worked on** — including products it did not create; each one carries its `origin` and the dialog groups on it.
- A SKU that exists and is **not ours** is skipped and never opened for edit.
- LLM / heuristic output is a **proposal**. `combination_skus.combination_map` is the decision.
- Telemetry (`DATABASE_URL`, run store) must never break product writes.
- Secrets stay on the server. There is **no web authentication** today — that is a known gap, not an implied feature.

Full tables with proof paths: [`ARCHITECTURE_INVARIANTS.md`](ARCHITECTURE_INVARIANTS.md).

## Index

| File | Role |
|---|---|
| [`CHANGE_PROCESS.md`](CHANGE_PROCESS.md) | The change loop agents must obey |
| [`ARCHITECTURE_INVARIANTS.md`](ARCHITECTURE_INVARIANTS.md) | Laws + proof + links |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Stack, layout, end-to-end path |
| [`LOCAL.md`](LOCAL.md) | Laptop setup (Windows / Mac): venv, `.env`, console, optional Postgres |
| [`AUTOMATION.md`](AUTOMATION.md) | Parser, runner, page objects, preview/commit, combinations |
| [`COLOUR.md`](COLOUR.md) | Three-tier colour codes, swatches, LLM contract |
| [`STORE.md`](STORE.md) | Postgres run store, ownership ledger, telemetry rule |
| [`WEB.md`](WEB.md) | FastAPI + static console contracts, secrets boundary |
| [`OBSERVABILITY.md`](OBSERVABILITY.md) | Error taxonomy, health vs ping, events |
| [`RAILWAY.md`](RAILWAY.md) | As-built Railway inventory and runbook (facts; re-verify IDs/URLs) |
| [`DEPLOYMENT-PLAN.md`](DEPLOYMENT-PLAN.md) | Why one container / Playwright image choices |
| [`../tests/README.md`](../tests/README.md) | Test map: which harness proves which law |
| [`../verification/REPO_MAP.md`](../verification/REPO_MAP.md) | Living pin/contract map |

## Living vs ephemeral

| Kind | Example | Agents may |
|---|---|---|
| Living | `/docs`, `verification/REPO_MAP.md`, Cursor rule | Trust after confirming code if conflict |
| Ephemeral | `var/`, `.env`, `report.xlsx`, pytest cache | gitignore; regenerate |

## Docs ship with behaviour

Any code change that affects flow or contracts must update the matching `/docs` file **in the same change set**. Encode that in the Cursor rule; do not leave docs for a follow-up.
