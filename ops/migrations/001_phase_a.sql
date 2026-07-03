-- Phase A schema: citation proof engine (Gate-2 loop)
-- Tenant tables carry org_id and enable RLS keyed on current_setting('app.org_id').
-- NOTE: superusers and table owners bypass RLS in local dev; on Supabase the app
-- role gets FORCE ROW LEVEL SECURITY (Phase C hardening, ADR-14). Policies exist now
-- so the worker's SET LOCAL discipline is exercised from day one.

create table orgs (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  created_at timestamptz not null default now()
);

create table sites (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  domain_norm text not null,
  is_control boolean not null default false,
  brand_terms text[] not null default '{}',  -- extra strings that count as a mention
  notes text,
  created_at timestamptz not null default now(),
  unique (org_id, domain_norm)
);

-- Immutable, versioned prompts: an edit is a new row with supersedes_id set.
create table tracked_prompts (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  prompt text not null,
  prompt_hash text not null,          -- md5(lower(trim(prompt)))
  engines text[] not null,            -- e.g. {openai,perplexity,gemini}
  active boolean not null default true,
  supersedes_id uuid references tracked_prompts(id),
  created_at timestamptz not null default now()
);
create unique index tracked_prompts_active_uq
  on tracked_prompts (site_id, prompt_hash) where active;

create table citation_runs (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  panel jsonb not null,               -- frozen [{prompt_id, prompt, engines}] at run creation
  scheduled_for timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  status text not null default 'pending'
    check (status in ('pending','running','done','failed')),
  created_at timestamptz not null default now()
);

create table citation_results (
  id bigint generated always as identity primary key,
  org_id uuid not null references orgs(id),
  run_id uuid not null references citation_runs(id),
  prompt_id uuid not null references tracked_prompts(id),
  engine text not null,
  engine_model_version text,
  sample_index int not null,
  sampled_at timestamptz not null default now(),
  cited boolean not null,
  cited_url text,
  mentioned boolean not null default false,
  answer_excerpt text,
  raw_ref text,                        -- path/key of stored raw response
  error text,                          -- non-null when the sample failed (excluded from rates)
  unique (run_id, prompt_id, engine, sample_index)
);

-- Gate-1 requirement: per-domain lever log (what changed, where, when).
create table levers (
  id bigint generated always as identity primary key,
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  applied_at date not null,
  lever_class text not null,           -- e.g. onsite_fix | directory_listing | schema | content
  description text not null,
  created_at timestamptz not null default now()
);

-- Durable job queue: claim-then-work with leases (ADR-11).
create table jobs (
  id bigint generated always as identity primary key,
  type text not null,
  org_id uuid,
  site_id uuid,
  payload jsonb not null default '{}',
  status text not null default 'queued'
    check (status in ('queued','running','done','failed','dead')),
  priority int not null default 5,
  run_after timestamptz not null default now(),
  attempts int not null default 0,
  max_attempts int not null default 3,
  idempotency_key text unique,
  concurrency_key text,
  locked_by text,
  locked_until timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  finished_at timestamptz
);
create index jobs_claim_idx on jobs (priority, run_after) where status = 'queued';
create index jobs_reaper_idx on jobs (locked_until) where status = 'running';

-- Catch-up scheduler (missed ticks run late, never never).
create table schedules (
  id uuid primary key default gen_random_uuid(),
  org_id uuid,
  site_id uuid,
  job_type text not null,
  payload jsonb not null default '{}',
  every_minutes int not null,
  next_run_at timestamptz not null default now(),
  enabled boolean not null default true,
  last_enqueued_at timestamptz
);

create table cost_events (
  id bigint generated always as identity primary key,
  org_id uuid,
  job_id bigint,
  provider text not null,
  purpose text not null,
  units jsonb not null default '{}',   -- e.g. {"prompt_tokens": 812, "completion_tokens": 240}
  cost_cents numeric(10,4) not null default 0,
  created_at timestamptz not null default now()
);

create table quota_ledgers (
  port text not null,
  scope text not null,                 -- e.g. engine name or site id
  date date not null,
  used int not null default 0,
  primary key (port, scope, date)
);

-- RLS on tenant tables (permissive to the setting; enforcement hardens on Supabase).
do $$
declare t text;
begin
  foreach t in array array['sites','tracked_prompts','citation_runs','citation_results','levers']
  loop
    execute format('alter table %I enable row level security', t);
    execute format(
      'create policy org_isolation on %I using (org_id = current_setting(''app.org_id'', true)::uuid)', t);
  end loop;
end $$;
