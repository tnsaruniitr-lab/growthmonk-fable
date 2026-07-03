# Phase D1 module contracts — booked-lead capture + WhatsApp trend card

The attribution spine: booked consults in (webhook + operator log), the weekly trend card out
(Meta Cloud API — the GrowthMonk WABA is proven infrastructure). Schema: migration 007.
Style/test rules per docs/phase-a-contracts.md; ZERO network in tests; local pg16 per the
phase-c-wave2 COMMON recipe. Privacy law: raw phone numbers NEVER stored — contact_ref is
sha256(wa_id) hex; message bodies stored only as a 120-char excerpt inside attribution.

## Agent A — gm/delivery/whatsapp.py + api webhook routes (owns api.py edits this wave)

```python
class WabaError(Exception): retryable
class WabaClient:
    def __init__(self, token=None, phone_number_id=None, client: httpx.Client | None = None)
        # env WABA_TOKEN / WABA_PHONE_NUMBER_ID; Graph API base https://graph.facebook.com/v20.0
    def send_text(self, to: str, body: str) -> dict          # POST /{pnid}/messages type=text
    def send_template(self, to: str, template: str, lang="en", components=None) -> dict
    # retry pattern local copy (429/5xx); 401/403 -> WabaError(retryable=False)

def verify_webhook(params: dict, verify_token: str) -> str | None
    # GET hub.mode/hub.verify_token/hub.challenge handshake -> challenge or None
def valid_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool
    # X-Hub-Signature-256 = 'sha256=' + HMAC-SHA256(app_secret, raw_body); constant-time compare
def parse_inbound(payload: dict) -> list[dict]
    # entry[].changes[].value.messages[] -> [{external_id: message.id, wa_id, ts, body_excerpt
    # (<=120 chars), referral (click-to-chat ad/source data when present), phone_number_id}]
    # defensive: statuses-only payloads -> []
```
api.py routes (append; keep existing style/guards):
- GET /webhooks/whatsapp: verify_webhook against env WABA_VERIFY_TOKEN (404 when env unset)
- POST /webhooks/whatsapp: 403 on bad signature (env WABA_APP_SECRET; when unset -> 404 —
  never accept unsigned webhooks silently); parse_inbound; map phone_number_id -> site via
  connections kind='whatsapp' meta {"phone_number_id": ...} (org context from that row);
  insert booked_leads (source='whatsapp', external_id dedupe ON CONFLICT DO NOTHING,
  contact_ref=sha256(wa_id), attribution={referral, body_excerpt}); ALWAYS 200 fast —
  processing errors are logged, never bounced to Meta (they retry aggressively).
Tests: handshake, signature valid/invalid/missing-env-404, inbound parse variants (message w/
referral, statuses-only, dedupe on replay), fastapi TestClient with DB skip guard for inserts.

## Agent B — gm/delivery/leadcard.py (card assembly + jobs + operator add)

```python
def add_lead(conn, *, org_id, site_id, source='manual', occurred_at=None, notes=None,
             attribution=None) -> str
def week_stats(conn, site_id, *, week_start: date) -> dict
    # {booked: n, prev_week: n, by_source: {...}, trend: 'up'|'down'|'flat'}
def build_card_text(conn, site_id, *, week_start: date) -> str
    # WhatsApp-ready plain text (<=1024 chars), 4 lines max:
    #   headline: "Booked consults this week: 7 (▲ from 4)"
    #   delta highlight: latest rank_movement improvement (lazy rank_tracker import, tolerate
    #     absence) OR most recent resolved finding OR latest receipt score movement
    #   next action: top open queue item by est_clicks_gain (kind + human name)
    #   footer: "GrowthMonk — reply STOP to pause"
    # Honest empty states ("No booked consults logged this week") — never invented numbers.
def handle_send_lead_card(ctx)   # job 'send_lead_card' (weekly schedule): builds the card,
    # sends via WabaClient.send_text to connections kind='whatsapp' meta.recipient_wa_id;
    # missing connection/env -> job fails with a clear message; the send is recorded as a
    # cost_event (provider='waba', purpose='lead_card', cost 0) for the audit trail
```
Tests: week math (Mon-start weeks, trend arrows, by_source), card text golden assertions incl.
every empty-state branch and the 1024-char cap, add_lead + handler flow with fake Waba under
the DB skip guard.

## Integrator wires (not agent-owned)
CLI: gm lead add / gm lead card [--send]; worker registers send_lead_card; connections
row via a small gm wa-connect command (phone_number_id + recipient, credentials NOT stored —
token lives in env only).
