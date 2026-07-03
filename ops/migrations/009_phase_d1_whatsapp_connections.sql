-- Phase D1: allow kind='whatsapp' connections (webhook phone_number_id -> site
-- mapping + lead-card recipient). Credentials stay NULL for these rows — the
-- WABA token lives in env only; meta carries
-- {"phone_number_id": ..., "recipient_wa_id": ...}.

alter table connections drop constraint if exists connections_kind_check;
alter table connections add constraint connections_kind_check
  check (kind in ('gsc','wordpress','ga4','google_ads','meta_ads','whatsapp'));
