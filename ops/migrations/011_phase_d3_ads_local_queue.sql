-- Phase D3: read-only AdsPort daily spend rows + local-presence queue kind.
--
-- ads_daily holds per-(site, date, channel, campaign) spend rows pulled by the
-- read-only AdsPort (gm/connections/ads.py). NO unique index on purpose: ingest
-- is slice replacement (DELETE the (site_id, date, channel) slice + INSERT —
-- gsc_daily's discipline), so re-pulls are idempotent by construction and
-- platforms restating conversions inside the trailing window just re-land.

create table ads_daily (
  id bigint generated always as identity primary key,
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  date date not null,
  channel text not null check (channel in ('google_ads','meta_ads')),
  campaign_id text not null default '',
  campaign_name text,
  spend numeric(12,2) not null default 0,
  currency text not null default 'AED',
  clicks int,
  platform_conversions numeric(12,2),   -- NULL = provider had nothing (honest absence)
  pulled_at timestamptz not null default now()
);
create index ads_daily_slice_idx on ads_daily (site_id, date desc, channel);

alter table ads_daily enable row level security;
create policy org_isolation on ads_daily
  using (org_id = current_setting('app.org_id', true)::uuid);

-- Allow local-presence check rows in the operator queue (mirrors 008/010).
-- The list carries 010's 'competitor_candidate' — dropping the constraint
-- without re-listing it would silently break the D2 discovery detector.
alter table queue_items drop constraint queue_items_kind_check;
alter table queue_items add constraint queue_items_kind_check check (
  kind in ('striking_distance','decay','ctr_outlier','cannibalization','keyword_gap',
           'competitor_candidate','local_presence')
);
