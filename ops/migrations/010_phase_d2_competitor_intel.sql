-- Phase D2: competitor intelligence pack — monthly competitor profiles, discovery
-- candidates in the operator queue, opt-in top-100 SERP depth.

create table competitor_profiles (
  id bigint generated always as identity primary key,
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  domain text not null,       -- normalized host; one of sites.competitor_domains
  checked_on date not null,   -- refresh day (monthly cadence)
  total_keywords int,         -- organic.count; NULL = provider had nothing (honest absence)
  top10_keywords int,         -- organic pos_1 + pos_2_3 + pos_4_10; NULL = provider had nothing
  est_traffic numeric,        -- organic.etv (bulk_traffic_estimation wins)
  movers jsonb not null default '{}',  -- {"new","up","down","lost"} from is_new/is_up/is_down/is_lost
  raw_metrics jsonb not null default '{}',
  provider text not null default 'dataforseo',
  cost_cents numeric(10,4) not null default 0,
  fetched_at timestamptz not null default now(),
  unique (site_id, domain, checked_on)  -- same-day re-runs upsert, idempotent
);

do $$
declare t text;
begin
  foreach t in array array['competitor_profiles']
  loop
    execute format('alter table %I enable row level security', t);
    execute format(
      'create policy org_isolation on %I using (org_id = current_setting(''app.org_id'', true)::uuid)', t);
  end loop;
end $$;

-- Allow competitor-discovery candidate rows in the operator queue (mirrors 008).
alter table queue_items drop constraint queue_items_kind_check;
alter table queue_items add constraint queue_items_kind_check check (
  kind in ('striking_distance','decay','ctr_outlier','cannibalization','keyword_gap',
           'competitor_candidate')
);

-- Opt-in top-100 SERP depth: default stays 10; 100 only via explicit `gm track` flags.
-- Snapshot cache honors depth: a row satisfies a request iff row.depth >= requested.
alter table tracked_queries add column serp_depth int not null default 10 check (serp_depth in (10,100));
alter table serp_snapshots add column depth int not null default 10;
