-- Phase D0: SERP rank tracking, AI Overview citations, keyword gap.

alter table sites add column if not exists competitor_domains text[] not null default '{}';
-- normalized hosts (ahrefs.com) — seeded manually or promoted from serp_comparisons

create table tracked_queries (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  query_norm text not null,
  target_page text,                     -- url the query should land on (optional)
  active boolean not null default true,
  created_at timestamptz not null default now(),
  unique (site_id, query_norm)
);

create table rank_history (
  id bigint generated always as identity primary key,
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  query_norm text not null,
  checked_on date not null,
  rank int,                             -- NULL = not in the tracked depth (honest absence)
  ranked_url text,
  aio_present boolean not null default false,
  aio_cited boolean not null default false,
  aio_cited_domains text[] not null default '{}',
  top_domains text[] not null default '{}',   -- ranks 1..10 fingerprint, in order
  snapshot_id uuid references serp_snapshots(id),
  unique (site_id, query_norm, checked_on)
);

do $$
declare t text;
begin
  foreach t in array array['tracked_queries','rank_history']
  loop
    execute format('alter table %I enable row level security', t);
    execute format(
      'create policy org_isolation on %I using (org_id = current_setting(''app.org_id'', true)::uuid)', t);
  end loop;
end $$;
