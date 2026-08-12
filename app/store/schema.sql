-- nopCommerce automation — run store
-- Apply once against Railway Postgres (or any Postgres): 
--   python sql.py app/store/schema.sql
-- Idempotent: safe to re-run.
--
-- Grains, deliberately separate: run -> row -> step.
--   Most UI questions are answered at row grain; most debugging happens at
--   step grain. Collapsing them into one events table makes both harder.
--
-- events is append-only NARRATIVE and is not the source of truth. Status lives
-- in typed columns so "show me every failed row" stays an indexed query rather
-- than a log scan.

create extension if not exists pgcrypto;

-- ─── enums ──────────────────────────────────────────────────────────────────
do $$ begin
  create type run_status as enum
    ('queued','claimed','running','succeeded','partial','failed','cancelled');
exception when duplicate_object then null; end $$;

-- 'previewed' is a dry-run row: checked and rehearsed, deliberately not saved.
-- It is NOT 'skipped' — a skipped row was left alone because it belongs to
-- someone else, and conflating the two makes a preview run read like a refusal.
do $$ begin
  create type row_status as enum
    ('pending','running','added','resumed','skipped','previewed','failed','blocked');
exception when duplicate_object then null; end $$;

do $$ begin
  create type step_status as enum ('running','ok','skipped','failed','retried');
exception when duplicate_object then null; end $$;

-- ─── runs: the session ──────────────────────────────────────────────────────
create table if not exists runs (
  id               uuid primary key default gen_random_uuid(),
  status           run_status  not null default 'queued',
  triggered_by     text,
  source_filename  text,
  source_sha256    text,
  target_base_url  text not null,
  commit           boolean not null default false,
  options          jsonb   not null default '{}',   -- redacted; never secrets
  worker_id        text,
  lease_expires_at timestamptz,
  total_rows       int not null default 0,
  counts           jsonb not null default '{}',
  error            text,
  created_at       timestamptz not null default now(),
  started_at       timestamptz,
  finished_at      timestamptz,
  duration_ms      bigint
);
create index if not exists runs_status_created_idx on runs (status, created_at desc);
create index if not exists runs_triggered_idx      on runs (triggered_by, created_at desc);

-- ─── run_rows: one product ──────────────────────────────────────────────────
create table if not exists run_rows (
  id            uuid primary key default gen_random_uuid(),
  run_id        uuid not null references runs(id) on delete cascade,
  sku           text not null,
  source_sheet  text,
  row_index     int  not null default 0,
  status        row_status not null default 'pending',
  payload       jsonb not null default '{}',
  product_id    text,
  product_url   text,
  attempt       int  not null default 0,
  error_class   text,
  error         text,
  colour_tiers  jsonb not null default '{}',   -- {map:9} — the tier-3 early warning
  started_at    timestamptz,
  finished_at   timestamptz,
  duration_ms   bigint,
  unique (run_id, sku)
);
create index if not exists run_rows_run_status_idx on run_rows (run_id, status);
create index if not exists run_rows_class_idx      on run_rows (error_class)
  where status = 'failed';

-- ─── row_steps: one action ──────────────────────────────────────────────────
create table if not exists row_steps (
  id          uuid primary key default gen_random_uuid(),
  run_id      uuid not null references runs(id) on delete cascade,
  row_id      uuid not null references run_rows(id) on delete cascade,
  name        text not null,
  status      step_status not null default 'running',
  attempt     int  not null default 1,
  error_class text,
  error       text,
  detail      jsonb not null default '{}',
  started_at  timestamptz not null default now(),
  duration_ms bigint
);
create index if not exists row_steps_row_idx  on row_steps (row_id, started_at);
create index if not exists row_steps_name_idx on row_steps (run_id, name, status);

-- ─── events: the narrative ──────────────────────────────────────────────────
create table if not exists events (
  id      bigserial primary key,
  run_id  uuid not null references runs(id) on delete cascade,
  row_id  uuid references run_rows(id) on delete cascade,
  step_id uuid references row_steps(id) on delete cascade,
  level   text not null default 'info',
  message text not null,
  data    jsonb not null default '{}',
  at      timestamptz not null default now()
);
create index if not exists events_run_idx on events (run_id, at);
create index if not exists events_row_idx on events (row_id, at) where row_id is not null;

-- ─── colour_codes: tier 1 ───────────────────────────────────────────────────
-- Scoped, not flat. The shipped payload has Hi-Vis Lime -> HLE on hard hats and
-- Hi-Viz Lime -> LE on vests: the same colour, a different code per family.
create table if not exists colour_codes (
  id           uuid primary key default gen_random_uuid(),
  scope        text not null default 'global',
  colour_raw   text not null default '',
  colour_key   text not null,
  code         text not null,
  source       text not null default 'human',   -- seed|heuristic|llm|human
  verified     boolean not null default false,
  confidence   real,
  times_used   bigint not null default 0,
  last_used_at timestamptz,
  created_at   timestamptz not null default now(),
  created_by   text,
  unique (scope, colour_key)
);
create index if not exists colour_codes_review_idx on colour_codes (verified, source);

-- ─── colour_swatches: which swatch a colour name means ──────────────────────
-- Separate from colour_codes because it answers a different question. A code is
-- derived from the row's own child SKUs and verified against them; a swatch is
-- a fact about the client's catalogue that nothing in the payload implies.
--
-- swatch_label is stored alongside the id and checked before use: ids are
-- per-instance keys, so an entry carried to another admin must fail loudly
-- rather than silently point at whatever swatch 25 is over there.
create table if not exists colour_swatches (
  id           uuid primary key default gen_random_uuid(),
  scope        text not null default 'global',
  colour_raw   text not null default '',
  colour_key   text not null,
  swatch_id    text not null,
  swatch_label text not null default '',
  source       text not null default 'human',   -- seed|human
  verified     boolean not null default false,
  created_at   timestamptz not null default now(),
  created_by   text,
  unique (scope, colour_key)
);

-- ─── llm_inferences: the audit trail ────────────────────────────────────────
create table if not exists llm_inferences (
  id                uuid primary key default gen_random_uuid(),
  run_id            uuid references runs(id) on delete set null,
  row_id            uuid references run_rows(id) on delete set null,
  scope             text,
  model             text not null,
  input             jsonb not null default '{}',
  output            jsonb,
  accepted          boolean,
  reject_reason     text,
  prompt_tokens     int,
  completion_tokens int,
  cost_usd          numeric(10,6),
  latency_ms        int,
  created_at        timestamptz not null default now()
);
create index if not exists llm_run_idx on llm_inferences (run_id, created_at);

-- ─── created_products: replaces var/created-products.json ───────────────────
-- Global and NOT per-run: it must outlive a run and be shared between
-- concurrent workers. The local JSON file it replaces had unlocked
-- read-modify-write, so two workers lost each other's entries — and a lost
-- entry means the "is this ours or the client's?" guard silently degrades.
-- The unique key on (base_url, sku) is what makes concurrent runs safe.
create table if not exists created_products (
  id          uuid primary key default gen_random_uuid(),
  base_url    text not null,
  sku         text not null,
  product_id  text,
  product_url text,
  run_id      uuid references runs(id) on delete set null,
  created_at  timestamptz not null default now(),
  deleted_at  timestamptz,
  unique (base_url, sku)
);

-- ─── views the UI reads ─────────────────────────────────────────────────────

-- Failures grouped by CAUSE, not by row. A 200-product run with 40 failures is
-- usually two problems; a flat list of 40 rows hides that completely.
create or replace view failure_summary as
select run_id,
       coalesce(error_class, 'unknown') as error_class,
       count(*)                          as failures,
       array_agg(sku order by sku)       as skus,
       min(error)                        as sample_error
from run_rows
where status = 'failed'
group by run_id, coalesce(error_class, 'unknown');

-- Per-step reliability: which stage is fragile, across all runs.
create or replace view step_health as
select name,
       count(*)                                          as attempts,
       count(*) filter (where status = 'ok')             as ok,
       count(*) filter (where status = 'failed')         as failed,
       round(avg(duration_ms))                           as avg_ms,
       percentile_cont(0.95) within group (order by duration_ms) as p95_ms
from row_steps
group by name;
