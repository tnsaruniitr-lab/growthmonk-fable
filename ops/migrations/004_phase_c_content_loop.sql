-- Phase C wave 2: SERP intelligence + the content loop (briefs -> drafts -> publish -> deltas).

create table serp_snapshots (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  query_norm text not null,
  engine text not null default 'google',
  location text not null default 'United Arab Emirates',
  results jsonb not null default '[]',   -- normalized organic [{rank,url,domain,title,type}]
  features jsonb not null default '[]',  -- serp feature list incl. PAA questions
  provider text not null default 'dataforseo',
  cost_cents numeric(10,4) not null default 0,
  fetched_at timestamptz not null default now()
);
create index serp_snapshots_lookup_idx on serp_snapshots (site_id, query_norm, fetched_at desc);

create table keyword_metrics (
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  query_norm text not null,
  volume int,
  cpc numeric(10,2),
  competition real,
  provider text not null default 'dataforseo',
  fetched_at timestamptz not null default now(),
  primary key (site_id, query_norm)
);

create table serp_comparisons (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  query_norm text not null,
  snapshot_id uuid references serp_snapshots(id),
  client_audit_id uuid references audits(id),
  competitor_audit_ids uuid[] not null default '{}',
  gaps jsonb not null default '[]',      -- [{check_id, client_status, competitors_passing, name}]
  summary jsonb not null default '{}',
  created_at timestamptz not null default now()
);

create table briefs (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  queue_item_id uuid references queue_items(id),
  source_audit_id uuid references audits(id),
  comparison_id uuid references serp_comparisons(id),
  serp_snapshot_ids uuid[] not null default '{}',
  target jsonb not null,                 -- {"query": ..., "page": ..., "kind": "new|refresh"}
  brief jsonb not null default '{}',
  status text not null default 'draft' check (status in ('draft','approved','used','discarded')),
  cost_cents numeric(10,4) not null default 0,
  created_at timestamptz not null default now()
);

-- The loop aggregate: one row per trip around brief->draft->publish->measure.
create table content_items (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  page_id uuid references pages(id),
  brief_id uuid references briefs(id),
  kind text not null check (kind in ('new','refresh')),
  status text not null default 'briefed' check (status in
    ('briefed','drafting','review','published','verified','measured','abandoned','verify_failed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index content_items_open_refresh_uq on content_items (page_id)
  where kind = 'refresh' and status not in ('measured','abandoned');

create table drafts (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  content_item_id uuid not null references content_items(id),
  version int not null default 1,
  package jsonb not null default '{}',   -- writer package (article json, jsonld, meta)
  body_ref text,                         -- object-storage/disk ref for rendered HTML
  scorecard_audit_id uuid references audits(id),
  human_todos jsonb not null default '[]',
  cost_cents numeric(10,4) not null default 0,
  created_at timestamptz not null default now(),
  unique (content_item_id, version)
);

create table publish_events (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  content_item_id uuid not null references content_items(id),
  target text not null,                  -- wordpress | export | webhook
  external_id text,
  url text,
  published_at timestamptz not null default now()
);

create table verify_events (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  content_item_id uuid not null references content_items(id),
  kind text not null check (kind in ('bev','inspection','schema')),
  result jsonb not null default '{}',
  at timestamptz not null default now()
);

-- Fix claims: enumerated up front so "resolved" on a receipt is a checked claim.
create table content_item_findings (
  content_item_id uuid not null references content_items(id),
  audit_finding_id bigint not null references audit_findings(id),
  intent text not null default 'fix' check (intent in ('fix','address')),
  primary key (content_item_id, audit_finding_id)
);

create table content_deltas (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  content_item_id uuid not null references content_items(id),
  publish_event_id uuid references publish_events(id),
  before_audit_id uuid references audits(id),
  after_audit_id uuid references audits(id),
  window_start date not null,
  window_end date not null,
  gsc_before jsonb not null default '{}',
  gsc_after jsonb not null default '{}',
  findings_diff jsonb not null default '{}',
  created_at timestamptz not null default now(),
  unique (content_item_id, window_start)
);

create table site_deltas (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  period text not null,                  -- e.g. '2026-08'
  payload jsonb not null default '{}',
  created_at timestamptz not null default now(),
  unique (site_id, period)
);

do $$
declare t text;
begin
  foreach t in array array['serp_snapshots','keyword_metrics','serp_comparisons','briefs',
    'content_items','drafts','publish_events','verify_events','content_deltas','site_deltas']
  loop
    execute format('alter table %I enable row level security', t);
    execute format(
      'create policy org_isolation on %I using (org_id = current_setting(''app.org_id'', true)::uuid)', t);
  end loop;
end $$;
