-- Phase C wave 1: connections + GSC measurement store + opportunity queue.

create table connections (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  kind text not null check (kind in ('gsc','wordpress','ga4','google_ads','meta_ads')),
  encrypted_credentials bytea,          -- sealed box (vault); NULL for reference-only conns
  key_version int not null default 1,
  status text not null default 'ok' check (status in ('ok','broken','revoked')),
  meta jsonb not null default '{}',     -- e.g. {"property": "sc-domain:example.com"}
  last_ok_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  unique (site_id, kind)
);

-- Raw GSC rows. Partitioned by month; ingest is slice-replacement per
-- (site_id, date, search_type) — DELETE slice + COPY, no upsert index needed.
create table gsc_daily (
  site_id uuid not null,
  date date not null,
  search_type text not null default 'web',
  page text not null,
  query text not null,
  clicks int not null default 0,
  impressions int not null default 0,
  ctr real not null default 0,
  position real not null default 0
) partition by range (date);
create index gsc_daily_site_date_idx on gsc_daily (site_id, date);

-- Phase-1 provisional aggregates (whole-window pulls; minutes after connect).
create table gsc_window_agg (
  site_id uuid not null,
  window_days int not null,             -- 28 | 90
  page text not null,
  query text not null,
  clicks int not null default 0,
  impressions int not null default 0,
  ctr real not null default 0,
  position real not null default 0,
  pulled_at timestamptz not null default now(),
  primary key (site_id, window_days, page, query)
);

-- Per-page daily rollup, incrementally rewritten per (site_id, date) slice.
create table gsc_page_daily (
  site_id uuid not null,
  date date not null,
  page text not null,
  clicks int not null default 0,
  impressions int not null default 0,
  position real not null default 0,     -- impression-weighted mean
  primary key (site_id, date, page)
);

-- Ingest bookkeeping: which dates are filled, which are final.
create table gsc_ingest_log (
  site_id uuid not null,
  date date not null,
  search_type text not null default 'web',
  rows int not null default 0,
  final boolean not null default false, -- dates < today-3 are final
  pulled_at timestamptz not null default now(),
  primary key (site_id, date, search_type)
);

create table page_url_history (
  id bigint generated always as identity primary key,
  org_id uuid not null references orgs(id),
  page_id uuid not null references pages(id),
  url_norm text not null,
  valid_from timestamptz not null default now(),
  valid_to timestamptz
);

-- The operator queue: detectors upsert, dismissal snoozes re-detection.
create table queue_items (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  kind text not null check (kind in ('striking_distance','decay','ctr_outlier','cannibalization')),
  page_id uuid references pages(id),
  target jsonb not null default '{}',   -- {"page": ..., "query": ...}
  target_hash text not null,
  at_stake jsonb not null default '{}', -- {"est_clicks_gain": ..., "basis": "provisional|final"}
  status text not null default 'open' check (status in ('open','actioned','dismissed','done')),
  snooze_until timestamptz,
  first_seen timestamptz not null default now(),
  last_seen timestamptz not null default now(),
  unique (site_id, kind, target_hash)
);

do $$
declare t text;
begin
  foreach t in array array['connections','queue_items','page_url_history']
  loop
    execute format('alter table %I enable row level security', t);
    execute format(
      'create policy org_isolation on %I using (org_id = current_setting(''app.org_id'', true)::uuid)', t);
  end loop;
end $$;
-- gsc_* tables are keyed by site_id only (hot ingest path, no org column):
-- workers always scope queries by site_id resolved through an org-scoped read.
