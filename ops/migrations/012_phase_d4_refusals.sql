-- Phase D4: refusal log (roadmap Phase D.7) — the >50% DIY-refusal early-death
-- tripwire needs data. One row per agency-pitch "no".

create table refusals (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  prospect text not null,                  -- who said no (clinic/agency name, free text)
  source text not null default 'agency_pitch',    -- pitch channel, free text
  reason text not null check (reason in ('diy','price','timing','trust','other')),
  notes text, refused_at date not null default current_date,
  created_at timestamptz not null default now()
);
create index refusals_org_time_idx on refusals (org_id, refused_at desc);

alter table refusals enable row level security;
create policy org_isolation on refusals
  using (org_id = current_setting('app.org_id', true)::uuid);
