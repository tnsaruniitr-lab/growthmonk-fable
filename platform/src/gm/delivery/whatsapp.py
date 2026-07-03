"""Meta WhatsApp Cloud API port — outbound sends + inbound lead capture (Phase D1).

Security posture (docs/phase-d1-contracts.md):

- Unsigned webhooks are NEVER accepted. The API layer 404s the POST route when
  WABA_APP_SECRET is unset (an unconfigured surface reads as absent, never as
  open acceptance), and `valid_signature` is a constant-time HMAC-SHA256 over
  the RAW request bytes — the caller must verify before any JSON parsing.
- Raw phone numbers are never persisted or logged: booked_leads.contact_ref is
  sha256(wa_id) hex, and message bodies survive only as a <=120-char excerpt
  inside the attribution jsonb.
- The webhook POST always answers 200 fast — processing failures are logged,
  never bounced to Meta (they retry aggressively on non-2xx and eventually
  disable the subscription).

Outbound: `WabaClient` wraps the Graph API /{phone_number_id}/messages endpoint
(the proven GrowthMonk WABA infrastructure). The retry/backoff pattern is a
local copy of the delivery-port convention (429/5xx/transport retried inside a
total time budget; 401/403 never retried).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import random
import time

import httpx
import psycopg
from psycopg.types.json import Jsonb

from gm import db

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v20.0"
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 3
BODY_EXCERPT_MAX = 120

# Module-level indirection so tests can patch out real sleeping.
_sleep = time.sleep


class WabaError(Exception):
    """WhatsApp Cloud API failure. `retryable` mirrors the jobs-layer convention."""

    def __init__(self, message: str, retryable: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


def _backoff_seconds(attempt: int, remaining: float) -> float:
    """Exponential backoff with jitter, capped by the remaining time budget."""
    base = min(8.0, 0.5 * (2**attempt))
    return max(0.0, min(base + random.uniform(0.0, base / 4), max(remaining, 0.0)))


class WabaClient:
    """Minimal Cloud API client for one WABA phone number (Bearer token auth)."""

    def __init__(self, token: str | None = None, phone_number_id: str | None = None,
                 client: httpx.Client | None = None):
        self.token = token or os.environ.get("WABA_TOKEN")
        self.phone_number_id = phone_number_id or os.environ.get("WABA_PHONE_NUMBER_ID")
        if not self.token or not self.phone_number_id:
            raise WabaError(
                "waba: WABA_TOKEN / WABA_PHONE_NUMBER_ID not configured — set both env vars "
                "or pass token/phone_number_id explicitly"
            )
        self._headers = {"Authorization": f"Bearer {self.token}"}
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)

    # -- transport ------------------------------------------------------------------

    def _post_messages(
        self,
        payload: dict,
        *,
        max_retries: int = MAX_RETRIES,
        total_budget_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """POST /{phone_number_id}/messages -> parsed JSON dict (local retry copy)."""
        url = f"{GRAPH_BASE}/{self.phone_number_id}/messages"
        deadline = time.monotonic() + total_budget_seconds
        attempt = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WabaError(
                    f"waba: {total_budget_seconds:.0f}s total budget exhausted", retryable=True
                )
            try:
                resp = self._client.post(
                    url, headers=self._headers, json=payload,
                    timeout=min(remaining, total_budget_seconds),
                )
            except httpx.HTTPError as exc:
                if attempt >= max_retries:
                    raise WabaError(
                        f"waba: transport failure after {attempt + 1} attempts: {exc}",
                        retryable=True,
                    ) from exc
                attempt += 1
                _sleep(_backoff_seconds(attempt, deadline - time.monotonic()))
                continue
            if resp.status_code in (401, 403):
                raise WabaError(
                    f"waba: HTTP {resp.status_code} authentication failure: {resp.text[:200]} "
                    f"— check WABA_TOKEN (expired system-user tokens are the usual cause)",
                    retryable=False, status_code=resp.status_code,
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= max_retries:
                    raise WabaError(
                        f"waba: HTTP {resp.status_code} after {attempt + 1} attempts",
                        retryable=True, status_code=resp.status_code,
                    )
                attempt += 1
                _sleep(_backoff_seconds(attempt, deadline - time.monotonic()))
                continue
            if resp.status_code >= 400:
                raise WabaError(
                    f"waba: HTTP {resp.status_code}: {resp.text[:500]}",
                    retryable=False, status_code=resp.status_code,
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise WabaError("waba: non-JSON response body", retryable=False) from exc
            if not isinstance(data, dict):
                raise WabaError(
                    f"waba: unexpected JSON payload type {type(data).__name__}", retryable=False
                )
            return data

    # -- API surface ----------------------------------------------------------------

    def send_text(self, to: str, body: str) -> dict:
        """Send a plain text message (inside the 24h service window)."""
        return self._post_messages({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"body": body},
        })

    def send_template(self, to: str, template: str, lang: str = "en",
                      components: list | None = None) -> dict:
        """Send an approved template message (required outside the 24h window)."""
        tpl: dict = {"name": template, "language": {"code": lang}}
        if components:
            tpl["components"] = components
        return self._post_messages({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": tpl,
        })


# ---------------------------------------------------------------------------
# Webhook verification + parsing (pure; the API layer wires them to routes)
# ---------------------------------------------------------------------------

def verify_webhook(params: dict, verify_token: str) -> str | None:
    """Meta GET subscription handshake: echo hub.challenge on a token match.

    Returns the challenge string to echo back, or None when the handshake is
    invalid (wrong mode, wrong token — compared constant-time — or no challenge).
    """
    if not verify_token:
        return None
    if params.get("hub.mode") != "subscribe":
        return None
    supplied = params.get("hub.verify_token")
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, verify_token):
        return None
    challenge = params.get("hub.challenge")
    return challenge if isinstance(challenge, str) else None


def valid_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Constant-time check of X-Hub-Signature-256 over the RAW body bytes.

    The header format is 'sha256=' + hex(HMAC-SHA256(app_secret, raw_body)).
    Callers MUST pass the exact bytes received on the wire (request.body()
    before any JSON parsing) — re-serialized JSON never matches.
    """
    if not app_secret or not header or not isinstance(header, str):
        return False
    prefix, _, provided = header.partition("=")
    if prefix.strip().lower() != "sha256" or not provided:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.strip().lower())


def contact_ref(wa_id: str) -> str:
    """sha256 hex of the sender id — the ONLY form a contact identity is stored in."""
    return hashlib.sha256(wa_id.encode("utf-8")).hexdigest()


def parse_inbound(payload: dict) -> list[dict]:
    """Flatten a Cloud API webhook payload into inbound-message events.

    Returns [{external_id, wa_id, ts, body_excerpt, referral, phone_number_id}]
    — one per message across all entries/changes. Defensive by design: statuses-
    only payloads yield [], missing referral is None, malformed fragments are
    skipped rather than raised (the webhook must never bounce on shape).
    """
    events: list[dict] = []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return events
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            pnid = metadata.get("phone_number_id") if isinstance(metadata, dict) else None
            messages = value.get("messages")
            if not isinstance(messages, list):
                continue  # statuses-only (delivery/read receipts) — nothing to capture
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                external_id = msg.get("id")
                wa_id = msg.get("from")
                if not external_id or not wa_id:
                    continue
                text = msg.get("text")
                body = text.get("body") if isinstance(text, dict) else None
                body = body if isinstance(body, str) else ""
                referral = msg.get("referral")
                try:
                    ts = int(msg.get("timestamp"))
                except (TypeError, ValueError):
                    ts = None
                events.append({
                    "external_id": str(external_id),
                    "wa_id": str(wa_id),
                    "ts": ts,
                    "body_excerpt": body[:BODY_EXCERPT_MAX],
                    "referral": referral if isinstance(referral, dict) else None,
                    "phone_number_id": str(pnid) if pnid else None,
                })
    return events


# ---------------------------------------------------------------------------
# Lead recording (called by the webhook POST route)
# ---------------------------------------------------------------------------

def record_inbound_leads(conn: psycopg.Connection, events: list[dict]) -> int:
    """Insert booked_leads rows for parsed inbound events; returns rows inserted.

    phone_number_id -> the connections row (kind='whatsapp', meta.phone_number_id)
    that carries the org/site context; unknown numbers are logged and dropped.
    Dedupe rides booked_leads.external_id ON CONFLICT DO NOTHING (webhook replays
    are a no-op). One transaction per event so SET LOCAL app.org_id is scoped to
    that event's own org. Raw wa_ids are hashed before touching the database and
    never logged.
    """
    inserted = 0
    for ev in events:
        pnid = ev.get("phone_number_id")
        external_id = ev.get("external_id")
        wa_id = ev.get("wa_id")
        if not pnid or not external_id or not wa_id:
            log.warning("whatsapp inbound: incomplete event %r dropped", external_id or "?")
            continue
        try:
            # Org-less lookup (like the share-token path): the connections row IS
            # the org context; scope is set from it before booked_leads is touched.
            row = conn.execute(
                "select org_id, site_id from connections"
                " where kind = 'whatsapp' and meta->>'phone_number_id' = %s",
                (str(pnid),),
            ).fetchone()
            if row is None:
                conn.rollback()
                log.warning(
                    "whatsapp inbound: no connection for phone_number_id %s — %r dropped",
                    pnid, external_id,
                )
                continue
            db.set_org(conn, row["org_id"])
            attribution: dict = {"body_excerpt": ev.get("body_excerpt") or ""}
            if ev.get("referral") is not None:
                attribution["referral"] = ev["referral"]
            got = conn.execute(
                "insert into booked_leads (org_id, site_id, source, occurred_at,"
                " external_id, contact_ref, attribution)"
                " values (%s, %s, 'whatsapp', coalesce(to_timestamp(%s), now()), %s, %s, %s)"
                " on conflict (external_id) do nothing returning id",
                (row["org_id"], row["site_id"], ev.get("ts"), external_id,
                 contact_ref(wa_id), Jsonb(attribution)),
            ).fetchone()
            conn.commit()
            if got is not None:
                inserted += 1
        except Exception:
            conn.rollback()
            log.exception("whatsapp inbound: failed to record %r", external_id)
    return inserted
