-- Phase B: pages, audits, findings, share tokens.
-- All grading lives in audits (ADR-16): page audits now, draft scorecards in Phase C
-- (draft_id column exists without FK until drafts arrive).

create table pages (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  url_norm text not null,
  page_type text,
  content_hash text,
  first_seen timestamptz not null default now(),
  last_crawled timestamptz,
  unique (site_id, url_norm)
);

create table audits (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  page_id uuid references pages(id),
  draft_id uuid,                        -- Phase C: FK added with the drafts table
  url text,                             -- audited URL as requested (pre-normalization)
  registry_version text not null,
  model_version text,
  status text not null default 'queued'
    check (status in ('queued','running','done','failed','inconclusive')),
  gate_state text,                      -- e.g. ok | transport_inconclusive | robots_blocked
  scores jsonb not null default '{}',   -- deterministic recompute output (PCR, sections, grade)
  cost_cents numeric(10,4) not null default 0,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz not null default now()
);
create index audits_site_idx on audits (site_id, created_at desc);

create table audit_findings (
  id bigint generated always as identity primary key,
  org_id uuid not null references orgs(id),
  audit_id uuid not null references audits(id),
  check_id text not null,
  check_version int not null,
  status text not null check (status in ('pass','warn','fail','na','inconclusive')),
  badge text not null,                  -- hard_evidence|measured|static_rule|comparative|heuristic|model_judgment
  fix_type text,
  evidence jsonb not null default '{}',
  citations jsonb not null default '[]',
  created_at timestamptz not null default now(),
  unique (audit_id, check_id)
);

create table report_shares (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  audit_id uuid not null references audits(id),
  token_hash text not null unique,      -- sha256 of the token; raw token never stored
  expires_at timestamptz not null default now() + interval '60 days',
  revoked boolean not null default false,
  created_at timestamptz not null default now()
);

do $$
declare t text;
begin
  foreach t in array array['pages','audits','audit_findings','report_shares']
  loop
    execute format('alter table %I enable row level security', t);
    execute format(
      'create policy org_isolation on %I using (org_id = current_setting(''app.org_id'', true)::uuid)', t);
  end loop;
end $$;
