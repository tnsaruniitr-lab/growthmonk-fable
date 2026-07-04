-- Phase D4 (WP-J): FORCE-RLS role split — script ONLY, no automatic prod cutover.
-- Closes the HANDOFF §2 debt: Railway's worker/api connect as the database OWNER,
-- and Postgres exempts table owners from RLS unless FORCE ROW LEVEL SECURITY is set.
-- This script creates a non-owner role `gm_worker` for the worker + api and forces
-- RLS on every org-scoped table, so the org_isolation policies actually bind.
--
-- Apply per ops/runbooks/rls-role-split.md (staged: scratch restore first, then prod).
-- NOT a migration on purpose: cutover is a deliberate operator action, and the
-- migration runner must keep connecting as the owner (it creates tables and policies).
-- Idempotent: safe to re-run.

-- 1. The role. LOGIN but no password yet — set it out-of-band, never in a committed
--    file: `alter role gm_worker password '...'` from an interactive psql.
do $$
begin
  if not exists (select from pg_roles where rolname = 'gm_worker') then
    create role gm_worker login;
  end if;
end $$;

-- 2. Grants: connect + schema usage + table CRUD + sequences, and matching default
--    privileges so tables created by FUTURE owner-run migrations are covered too.
--    (alter default privileges applies to objects later created by the role running
--    this script — run it as the migration owner, which the runbook does.)
do $$
begin
  execute format('grant connect on database %I to gm_worker', current_database());
end $$;
grant usage on schema public to gm_worker;
grant select, insert, update, delete on all tables in schema public to gm_worker;
grant usage, select on all sequences in schema public to gm_worker;
alter default privileges in schema public
  grant select, insert, update, delete on tables to gm_worker;
alter default privileges in schema public
  grant usage, select on sequences to gm_worker;

-- 3. FORCE RLS on every table carrying an org_isolation policy (explicit list —
--    grep 'org_isolation' ops/migrations/*.sql; keep in sync with new migrations):
--    001: sites, tracked_prompts, citation_runs, citation_results, levers
--    002: pages, audits, audit_findings, report_shares
--    003: connections, queue_items, page_url_history
--    004: serp_snapshots, keyword_metrics, serp_comparisons, briefs, content_items,
--         drafts, publish_events, verify_events, content_deltas, site_deltas
--    006: tracked_queries, rank_history        007: booked_leads
--    010: competitor_profiles                  011: ads_daily
--    012: refusals
--    Deliberately NOT listed (no org_isolation policy; the worker reads them
--    org-less): orgs, jobs, schedules, cost_events, quota_ledgers, schema_migrations,
--    gsc_daily, gsc_window_agg, gsc_page_daily, gsc_ingest_log, content_item_findings.
alter table sites                 force row level security;
alter table tracked_prompts       force row level security;
alter table citation_runs         force row level security;
alter table citation_results      force row level security;
alter table levers                force row level security;
alter table pages                 force row level security;
alter table audits                force row level security;
alter table audit_findings        force row level security;
alter table report_shares         force row level security;
alter table connections           force row level security;
alter table queue_items           force row level security;
alter table page_url_history      force row level security;
alter table serp_snapshots        force row level security;
alter table keyword_metrics       force row level security;
alter table serp_comparisons      force row level security;
alter table briefs                force row level security;
alter table content_items         force row level security;
alter table drafts                force row level security;
alter table publish_events        force row level security;
alter table verify_events         force row level security;
alter table content_deltas        force row level security;
alter table site_deltas           force row level security;
alter table tracked_queries       force row level security;
alter table rank_history          force row level security;
alter table booked_leads          force row level security;
alter table competitor_profiles   force row level security;
alter table ads_daily             force row level security;
alter table refusals              force row level security;

-- 4. Verification (run and eyeball; the two SELECT probes below must run AS
--    gm_worker — the owner may still bypass via superuser).
--
-- 4a. Every listed table must show relrowsecurity = t AND relforcerowsecurity = t:
--   select relname, relrowsecurity, relforcerowsecurity
--     from pg_class
--    where relnamespace = 'public'::regnamespace
--      and relname in ('sites','tracked_prompts','citation_runs','citation_results',
--        'levers','pages','audits','audit_findings','report_shares','connections',
--        'queue_items','page_url_history','serp_snapshots','keyword_metrics',
--        'serp_comparisons','briefs','content_items','drafts','publish_events',
--        'verify_events','content_deltas','site_deltas','tracked_queries',
--        'rank_history','booked_leads','competitor_profiles','ads_daily','refusals')
--    order by relname;
--
-- 4b. As gm_worker, a cross-org context must see NOTHING (0 rows):
--   begin;
--   set local app.org_id = '00000000-0000-0000-0000-000000000001';  -- not a real org
--   select id from sites limit 5;      -- MUST return 0 rows
--   rollback;
--
-- 4c. As gm_worker, NO org context must see NOTHING (0 rows — current_setting's
--     missing_ok returns NULL, and org_id = NULL is never true):
--   select id from sites limit 5;      -- MUST return 0 rows
