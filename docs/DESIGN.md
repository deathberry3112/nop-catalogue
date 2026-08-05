# Design: colour resolution, run store, observability, concurrency

Status: **draft for discussion**. Nothing here is built yet.
Scope: what to build next and why. Implementation is a separate conversation.

Three things are proposed, in dependency order:

1. **Colour resolution** becomes a three-tier pipeline (map → heuristic → LLM),
   with every resolution recorded and reusable.
2. **A Supabase run store** replaces the local JSON ledger and the print-based
   reporting, so a run is a queryable object rather than a terminal scrollback.
3. **Concurrency** becomes safe, which is mostly a consequence of (2) — the
   thing blocking it today is that "what have we created" lives in a local file.

The guiding constraint throughout: **this automation writes to a production
catalogue.** A wrong SKU is expensive to find and expensive to unwind. Every
design choice below prefers a loud failure over a confident guess.

---

## Part A — Colour resolution

### A.1 What the problem actually is

Worth stating precisely, because it changes what the LLM is for.

The payload gives combination SKUs as opaque strings (`"CP-V-200 LE 2XL"`) and a
separate list of colours (`["Lime", "Orange"]`). It never says which is which.
But `split_combination()` already *extracts* the code set from those strings.

So the task is **not** "generate a code for Yellow". It is:

> Given colours `{Lime, Orange}` and codes `{LE, OE}` — both closed, both known,
> both the same size — produce the bijection.

That has three consequences:

- **It is verifiable.** `combination_map()` already asserts that every payload
  combination is consumed exactly once and that the count equals
  `colours × sizes`. A wrong assignment fails this check. **The LLM proposes;
  the existing invariants dispose.** We never write an unverified guess.
- **It is small.** Never more than ~10 items per side. One cheap model call.
- **It is not "colour knowledge".** The model is doing constrained matching, not
  recalling a colour taxonomy. Prompt and evaluate it as such.

The current heuristic (`combination_skus.score`) already does this greedily and
gets **13/13 on the current payload with zero overrides**. The tiers below exist
for the colours we haven't seen yet, not to fix a present failure.

### A.2 The three tiers

| Tier | Source | When it fires | Latency | Trust |
|---|---|---|---|---|
| 1 | Curated map (DB) | exact `(scope, colour)` hit | ~0 | highest — human-confirmed |
| 2 | Heuristic scorer | map miss | ~0 | high — verified against payload |
| 3 | OpenAI | heuristic ambiguous/zero | ~500ms | **proposal only** — must pass verification |
| — | Fail | tier 3 off, errors, or verification fails | — | row marked Failed, nothing written |

Resolution is per-row, and tiers are applied per-colour, not all-or-nothing: a
row with 8 map hits and 1 unknown makes one LLM call about one colour, with the
8 known codes excluded from the candidate set. That constraint alone usually
collapses the answer to a single option.

**Tier 3 never writes to tier 1 automatically.** An accepted LLM answer lands in
the map as `source='llm', verified=false`. It is used, and it is surfaced in a
review queue (§E.3). A human promoting it to `verified=true` is what makes it
authoritative. Rationale: verification proves the assignment is *self-consistent
with this payload*, which is strong but not the same as *correct* — two colours
could be swapped and still satisfy the count checks if their codes are equally
plausible. Human confirmation closes that gap; the queue makes it a 5-second job.

### A.3 Scoping — the part that matters most

From the current payload:

```
HardHat  Hi-Vis Lime   -> HLE      Hi-Vis Orange -> HOE
Vest     Hi-Viz Lime   -> LE       Hi-Viz Orange -> OE
Vest     Lime          -> LE       Orange        -> OE
```

Same colours, different codes, distinguished today only by a `Vis`/`Viz`
spelling difference that nobody promised would persist.

**Therefore the map key is `(scope, colour_normalised)`, not `colour`.**

- `scope` = the source sheet or product family (`HardHat`, `Vest`), taken from
  the row's `source_sheet`, which the payload already carries.
- Lookup order: exact `(scope, colour)` → `(global, colour)` → tier 2.
- `colour_normalised` = casefold + collapse whitespace + strip punctuation, so
  `"Hi-Vis Lime"`, `"hi vis lime"` and `"Hi-Vis  Lime"` are one key. **Vis/Viz is
  deliberately NOT normalised away** — on this data they are genuinely different
  entries, and merging them would silently pick one code for both.
- A `(global, ...)` entry is only created by a human, never by tier 3. Tier 3
  writes are always scoped, because it has no basis to generalise.

Seeded from the current payload — 13 entries, all machine-derived, all pending
human confirmation:

```
HardHat: Yellow YW, White WE, Blue BE, Green GN, Pink PK, Red RD, Grey GY,
         Hi-Vis Orange HOE, Hi-Vis Lime HLE
Vest:    Hi-Viz Lime LE, Hi-Viz Orange OE, Lime LE, Orange OE
```

> **Note on `Yellow`:** you mentioned `YH`; the payload says **`YW`**. Going with
> the payload. Flagging in case `YH` is from a sheet I haven't seen — if so, that
> is a second scope and good early evidence for this whole section.

### A.4 Honest assessment of the map's value

On current data the map is **redundant for correctness** — the heuristic already
gets everything. Its real value is:

1. **Regression pinning.** A confirmed entry means a future tweak to `score()`
   cannot silently change an answer that was right.
2. **A write target.** Tier 3 and human decisions need somewhere to land, and
   that place must be the DB, not `OVERRIDES` in source — otherwise resolving a
   colour requires a deploy.
3. **Auditability.** "Why did this product get SKU X" becomes a row lookup.

It is worth building for those reasons. It is not worth pretending it fixes a
problem we currently have.

### A.5 LLM contract

- Model: `gpt-4o-mini`. Adequate for constrained matching; escalate only if the
  measured accept rate (§C) says otherwise.
- **Structured output**, strict schema: `{assignments: [{colour, code, confidence, reasoning}]}`.
- Input: the colour list, the *remaining* code list, the scope, and 3–5
  confirmed examples from the same scope. No product data, no SKUs, no
  credentials.
- `temperature=0`. Timeout 10s, 2 retries on transport error only.
- **Output is a proposal.** It goes through `combination_map()` verification
  exactly like the heuristic's. Failure = the row fails, the same as today.
- Kill switch: absent `OPENAI_API_KEY`, tier 3 is skipped and behaviour is
  identical to today. Never a hard dependency.
- Every call logged to `llm_inferences` (§B) with tokens, cost, latency, and
  whether the proposal survived verification.

---

## Part B — Supabase schema

Design notes before the DDL:

- **`runs` is the session.** Your `sessionID` is `runs.id` (uuid). Everything
  hangs off it.
- **Three grains, deliberately separate:** run → row → step. Most UI questions
  are answered at row grain; most debugging happens at step grain. Collapsing
  them into one events table makes both harder.
- **`events` is append-only and is not the source of truth.** Status lives in
  typed columns on `run_rows`/`row_steps`; events are the narrative. This keeps
  "show me all failed rows" a fast indexed query rather than a log scan.
- **JSONB for payloads, columns for anything you filter or aggregate on.** The
  temptation to put status in JSONB should be resisted.
- Timestamps are `timestamptz`, always. Durations are stored, not derived at
  read time — you will aggregate on them constantly.

```sql
-- ─── run (session) ────────────────────────────────────────────────────────
create type run_status as enum
  ('queued','claimed','running','succeeded','partial','failed','cancelled');

create table runs (
  id              uuid primary key default gen_random_uuid(),
  status          run_status not null default 'queued',
  triggered_by    text,                    -- user id / 'schedule' / 'api'
  source_filename text,
  source_sha256   text,                    -- identical upload detection
  target_base_url text not null,
  commit          boolean not null,        -- false = dry run
  options         jsonb not null default '{}',   -- RunOptions snapshot, no secrets
  worker_id       text,                    -- which container owns it
  lease_expires_at timestamptz,            -- crash detection (§F)
  total_rows      int  not null default 0,
  counts          jsonb not null default '{}',   -- {added,failed,skipped,pending}
  error           text,                    -- only for whole-run aborts
  created_at      timestamptz not null default now(),
  started_at      timestamptz,
  finished_at     timestamptz,
  duration_ms     bigint
);
create index on runs (status, created_at desc);
create index on runs (triggered_by, created_at desc);

-- ─── row (one product) ────────────────────────────────────────────────────
create type row_status as enum
  ('pending','running','added','resumed','skipped','failed','blocked');

create table run_rows (
  id            uuid primary key default gen_random_uuid(),
  run_id        uuid not null references runs(id) on delete cascade,
  sku           text not null,
  source_sheet  text,
  row_index     int  not null,             -- position in the workbook
  status        row_status not null default 'pending',
  payload       jsonb not null,            -- the parsed row, verbatim
  product_id    text,                      -- nop's id once known
  product_url   text,
  attempt       int  not null default 0,
  error_class   text,                      -- §D taxonomy
  error         text,
  started_at    timestamptz,
  finished_at   timestamptz,
  duration_ms   bigint,
  unique (run_id, sku)
);
create index on run_rows (run_id, status);
create index on run_rows (error_class) where status = 'failed';

-- ─── step (one action within a row) ───────────────────────────────────────
create type step_status as enum ('running','ok','skipped','failed','retried');

create table row_steps (
  id          uuid primary key default gen_random_uuid(),
  run_id      uuid not null references runs(id) on delete cascade,
  row_id      uuid not null references run_rows(id) on delete cascade,
  name        text not null,               -- 'create'|'spec'|'attribute'|...
  status      step_status not null,
  attempt     int not null default 1,
  error_class text,
  error       text,
  detail      jsonb not null default '{}', -- step-specific metrics
  started_at  timestamptz not null default now(),
  duration_ms bigint
);
create index on row_steps (row_id, started_at);
create index on row_steps (run_id, name, status);

-- ─── narrative log ────────────────────────────────────────────────────────
create table events (
  id        bigserial primary key,
  run_id    uuid not null references runs(id) on delete cascade,
  row_id    uuid references run_rows(id) on delete cascade,
  step_id   uuid references row_steps(id) on delete cascade,
  level     text not null,                 -- debug|info|warn|error
  message   text not null,
  data      jsonb not null default '{}',
  at        timestamptz not null default now()
);
create index on events (run_id, at);
create index on events (row_id, at) where row_id is not null;

-- ─── the colour map (tier 1) ──────────────────────────────────────────────
create table colour_codes (
  id            uuid primary key default gen_random_uuid(),
  scope         text not null default 'global',   -- 'HardHat' | 'Vest' | 'global'
  colour_raw    text not null,             -- as written in the sheet
  colour_key    text not null,             -- normalised (§A.3)
  code          text not null,
  source        text not null,             -- seed|heuristic|llm|human
  verified      boolean not null default false,
  confidence    real,                      -- tier 3 only
  times_used    bigint not null default 0,
  last_used_at  timestamptz,
  created_at    timestamptz not null default now(),
  created_by    text,
  unique (scope, colour_key)
);
create index on colour_codes (verified, source);

-- ─── LLM audit ────────────────────────────────────────────────────────────
create table llm_inferences (
  id            uuid primary key default gen_random_uuid(),
  run_id        uuid references runs(id) on delete set null,
  row_id        uuid references run_rows(id) on delete set null,
  scope         text,
  model         text not null,
  input         jsonb not null,            -- colours + candidate codes + examples
  output        jsonb,
  accepted      boolean,                   -- did it survive verification?
  reject_reason text,
  prompt_tokens int, completion_tokens int, cost_usd numeric(10,6),
  latency_ms    int,
  created_at    timestamptz not null default now()
);

-- ─── created-product ledger (replaces var/created-products.json) ──────────
-- Global, NOT per-run: the whole point is that it outlives a run and is shared
-- between concurrent workers. This is what makes §F safe.
create table created_products (
  sku          text primary key,
  base_url     text not null,
  product_id   text,
  product_url  text,
  run_id       uuid references runs(id) on delete set null,
  created_at   timestamptz not null default now(),
  deleted_at   timestamptz
);
```

**RLS:** service key writes from the worker; the UI reads through policies keyed
on `triggered_by`. Locking this down before the UI exists is cheap; retrofitting
it is not.

**Secrets:** `runs.options` is a filtered snapshot. `Credentials` never goes near
this DB — `password` is already `repr=False` in code and must not be re-exposed
by serialising the object wholesale.

**Retention:** `events` grows fastest. Propose 30 days for `events` on succeeded
runs, 180 days on failed ones (failures are what you go back to), indefinite for
`runs`/`run_rows` summaries. A nightly `pg_cron` job, not a manual chore.

---

## Part C — Metrics that actually matter

Deliberately short. Every metric below has an action attached to it; anything
that doesn't have one is a vanity number and is excluded.

**Throughput / capacity**

| Metric | Why | Action it drives |
|---|---|---|
| p50 / p95 **seconds per product**, split by combination count | The only number that predicts a batch's runtime | Capacity planning; sets concurrency target |
| **Passes per combination** in `apply_combination_skus` | Ideal is 1.0. Today the loop re-reads the whole grid every pass — O(n²) | If >1.3, the grid-rebind retry is the bottleneck, not the network |
| Products/hour per worker | Sizing | How many containers |

**Reliability**

| Metric | Why | Action it drives |
|---|---|---|
| Failure rate **by `error_class`** (§D), not by row | 40 rows failing for one reason is one problem | Tells you what to fix next, ranked |
| Failure rate **by step name** | Which stage is fragile | My prediction: `combinations` dominates |
| **Retry success rate** per error class | A class that never recovers should not be retried | Delete pointless retries; they triple runtime |
| Selector-miss rate per `first_visible` candidate list | Early warning of a nop version drift | Which selectors to re-verify |

**Colour resolution — the new surface**

| Metric | Why | Action it drives |
|---|---|---|
| **Tier distribution** (map / heuristic / llm / fail) | The leading indicator | Rising tier-3 share = the map needs curating |
| LLM **accept rate** (survived verification) | Whether the model is useful | <80% → change prompt or model, or turn tier 3 off |
| Unverified map entries outstanding | Human-in-the-loop debt | Sizes the review queue |
| Cost per run | Should be ~$0 | Alerts on runaway calls |

**Baseline to capture before any of this ships:** current per-product duration
and current tier distribution (which is 100% heuristic). Without a before, you
cannot prove an after.

---

## Part D — Error taxonomy and retries

Today every failure is a string in a list. That is why failure triage is
manual. **The single highest-leverage change in this document is classifying
errors**, because retry policy, UI grouping, and the failure-rate metrics all
depend on it and none of them work without it.

`error_class` values, and what each implies:

| Class | Example | Retry? | Whose problem |
|---|---|---|---|
| `transient_dom` | Kendo grid rebound, element detached | **Yes**, immediate ×5 | Nobody — expected |
| `transient_net` | Timeout, 502 from the admin | **Yes**, backoff ×3 | Infra |
| `auth` | Session expired mid-run | **Yes**, once, after re-login | Nobody |
| `selector_miss` | Element never appeared | **No** | Us — nop changed |
| `data_colour` | Colour unresolvable at all 3 tiers | **No** | Client — sheet or map |
| `data_invalid` | Payload failed validation | **No** | Client — sheet |
| `nop_rejected` | Admin returned a validation error | **No** | Client — bad value |
| `conflict` | SKU claimed by another run | **No** | Expected under concurrency |
| `unknown` | Anything unmapped | **No** | Us — triage and classify it |

Two rules that follow, and that are worth holding to:

1. **Never retry a deterministic failure.** Retrying `data_colour` five times
   burns five minutes to reach the same answer. The current code retries broadly
   because it cannot tell the difference; classification is what fixes that.
2. **Retry at the step, not the row.** Re-running a whole product to recover one
   combination edit is wasteful and re-exercises the write path unnecessarily.
   The step-level idempotency to make this safe already exists.

A growing `unknown` count is itself the metric: it means the taxonomy is stale.

---

## Part E — UI / UX

Principles, ordered by how much they matter:

### E.1 Group failures by cause, never by row
The default failure view is `error_class` → count → affected SKUs, sorted by
count. A 200-product run with 40 failures is usually two problems, and a flat
list of 40 rows hides that completely. One click expands to rows.

### E.2 Progress must be honest about what remains
Not a spinner. `47/200 · 3 failed · ~18 min left`, with the estimate from
measured p50-per-product for *this* run, not a global average — combination
count dominates duration and varies wildly per row (5 vs 16 in your payload).

### E.3 The colour review queue
A dedicated inbox for `colour_codes where verified = false`. Each item shows the
colour, the proposed code, the scope, which tier proposed it, and the payload
combinations it was derived from. Two buttons: **Confirm** / **Correct**.
This is the human-in-the-loop that makes tier 3 safe, and it must be a 5-second
interaction or it will not get used.

### E.4 Resume is a first-class button, not a re-upload
The idempotency to support this already exists end to end. The UI should expose
"retry failed rows" on a finished run directly, rather than making the user
download the report and upload it again. Keep the report round-trip — it is a
good escape hatch — but it should not be the primary path.

### E.5 Dry-run is the default in the UI
`commit=false` already does everything except the final save. Make the primary
button "Preview", with "Commit" as the confirmed second step. Cheap protection
against the failure mode that actually hurts: a bad sheet run against production.

---

## Part F — Concurrency

**There is a correctness blocker to clear first.** `CreatedLedger` is a local
JSON file (`var/created-products.json`) with read-modify-write and no locking.
Two workers on the same catalogue will interleave writes and lose entries. A
lost ledger entry means the delete/cleanup path no longer knows it owns a
product, and the "is this ours or the client's?" guard — the thing standing
between this automation and someone else's catalogue data — silently degrades.

**Migrating the ledger to `created_products` (§B) is a prerequisite for
concurrency, not a nice-to-have.** With it in Postgres, the guard becomes a
unique constraint and the race disappears.

Given that, concurrency is straightforward because rows are already independent:

- **Claim runs** with `select ... for update skip locked` + a `lease_expires_at`
  heartbeat. A worker that dies has its lease expire and the run is reclaimed —
  and because every step is idempotent, reclaiming is safe.
- **Parallelism within a run** is the bigger win: shard rows across N browser
  contexts. Contexts are independent and already constructed by
  `build_context_kwargs`. Start at **4 contexts per worker**, tune from the
  measured per-product p95 — the admin is the shared resource and it will be the
  first thing to complain.
- **Cross-run SKU safety:** `unique(sku)` on `created_products` plus an
  advisory lock on `hash(base_url || sku)` for the create window. Two runs
  containing the same SKU then produce one product and one `conflict`, which is
  the correct outcome and is visible in the UI rather than silent.
- **Same-target throttling:** cap total concurrent contexts *per `base_url`*,
  not per worker. Ten workers against one nop instance will take it down, and
  that is a client-visible outage.
- **Isolate per run:** browser profiles keyed by `run_id`.

---

## Part G — Suggested sequence

Ordered by (value ÷ risk), and each phase is independently shippable.

| Phase | Contents | Unblocks |
|---|---|---|
| **0** | Error taxonomy (§D). Classify at the raise site. | Every metric and UI grouping below |
| **1** | Supabase schema + write runs/rows/steps/events. Migrate the ledger. | Observability; concurrency prerequisite |
| **2** | Colour map tier 1 + seed 13 entries + record tier used | Baseline tier distribution |
| **3** | Extract `run_batch` into `app/automation/runner.py` | Production path stops being a pytest test |
| **4** | UI: run list, row drill-down, grouped failures | Client-visible value |
| **5** | Tier 3 (OpenAI) + review queue | Handles unseen colours |
| **6** | Concurrency: leases, sharded contexts, per-target throttle | Scale |

Phase 0 before everything is the argument I'd most want to defend: it is a small
change that every later phase depends on, and retrofitting classification across
an existing UI and metrics layer is significantly more work than adding it now.

Phase 3 is placed after 1–2 rather than first because the extraction is easier
once the run store exists to write to — otherwise it gets written twice.

**Note on ordering vs. your framing:** you put the OpenAI migration first. I've
put it at phase 5 because on current data it fires 0% of the time, and the map
plus tier-distribution metrics (phase 2) are what tell us whether it is needed
at all — and how to prompt it when it is. If you'd rather prove the LLM path
early, phase 5 can move up to run parallel with phase 2; it is genuinely
independent. It just isn't urgent on the evidence we have.

---

## Open questions

1. **Is `YH` a real code for Yellow in a sheet I haven't seen?** If so it is a
   third scope and directly validates §A.3 — please send that sheet.
2. **Is the code grammar stable across product families?** The vest rows drop
   the `Hi-Viz` qualifier entirely (`LE`, not `HLE`). Is that a vest convention
   or a data-entry inconsistency? Changes whether scope is a workaround or the
   correct model.
3. **What is a realistic batch size and cadence?** 7 rows vs 700 vs 7,000
   changes the concurrency target and the retention policy materially.
4. **Attributes beyond colour and size** — still the largest open design item,
   and it needs a sample sheet before it can be costed (see previous analysis).
5. **Who operates the UI** — your team, or the client directly? Drives the RLS
   model and how much nopCommerce vocabulary the UI can assume.
6. **One nop instance or several?** `base_url` is already threaded through
   everywhere, but per-target throttling and the SKU uniqueness scope both
   depend on the answer.
