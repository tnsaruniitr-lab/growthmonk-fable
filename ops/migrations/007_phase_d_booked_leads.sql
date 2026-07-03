-- Phase D: booked-lead capture — the attribution denominator (research module #2).

create table booked_leads (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  site_id uuid not null references sites(id),
  source text not null check (source in ('whatsapp','call','manual','booking_system')),
  occurred_at timestamptz not null default now(),
  external_id text unique,              -- e.g. WhatsApp message id (webhook idempotency)
  contact_ref text,                     -- sha256 of the sender id — never the raw number
  attribution jsonb not null default '{}',  -- e.g. {"referral": {...click-to-chat...}, "body_excerpt": ...}
  notes text,
  created_at timestamptz not null default now()
);
create index booked_leads_site_time_idx on booked_leads (site_id, occurred_at desc);

alter table booked_leads enable row level security;
create policy org_isolation on booked_leads
  using (org_id = current_setting('app.org_id', true)::uuid);
