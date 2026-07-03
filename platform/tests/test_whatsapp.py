"""Tests for gm.delivery.whatsapp + the /webhooks/whatsapp API routes.

Signature/handshake/parsing and WabaClient tests always run (httpx.MockTransport,
no network, no DB — the POST route's no-DB paths use a poisoned _connect).
Lead recording end-to-end (insert + replay dedupe + privacy assertions) runs
only under DATABASE_URL.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from gm import api, db
from gm.delivery import whatsapp

needs_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")

client = TestClient(api.app)

SECRET = "test-app-secret"
PNID = "108765432109876"
WA_ID = "9715550001234"


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _msg_payload(*, msg_id: str = "wamid.A1", wa_id: str = WA_ID,
                 body: str = "hi, I want a consult", referral: dict | None = None,
                 pnid: str = PNID, ts: str | None = "1719990000") -> dict:
    msg: dict = {"id": msg_id, "from": wa_id, "type": "text", "text": {"body": body}}
    if ts is not None:
        msg["timestamp"] = ts
    if referral is not None:
        msg["referral"] = referral
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA_ID",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "15550001", "phone_number_id": pnid},
                    "contacts": [{"wa_id": wa_id, "profile": {"name": "Test"}}],
                    "messages": [msg],
                },
            }],
        }],
    }


STATUSES_ONLY = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "WABA_ID",
        "changes": [{
            "field": "messages",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {"phone_number_id": PNID},
                "statuses": [{"id": "wamid.S1", "status": "delivered",
                              "recipient_id": WA_ID}],
            },
        }],
    }],
}

REFERRAL = {
    "source_url": "https://fb.me/xyz",
    "source_type": "ad",
    "source_id": "1234",
    "headline": "Book a consult",
}


class _PoisonConn:
    """A _connect() replacement that fails the test if the DB is ever touched."""

    def __call__(self):
        raise AssertionError("DB must not be touched on this path")


# ---------------------------------------------------------------------------
# verify_webhook (pure)
# ---------------------------------------------------------------------------


def test_verify_webhook_happy_path():
    params = {"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "12345"}
    assert whatsapp.verify_webhook(params, "tok") == "12345"


def test_verify_webhook_rejects_bad_token_mode_and_missing_challenge():
    good = {"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "c"}
    assert whatsapp.verify_webhook({**good, "hub.verify_token": "nope"}, "tok") is None
    assert whatsapp.verify_webhook({**good, "hub.mode": "unsubscribe"}, "tok") is None
    assert whatsapp.verify_webhook({"hub.mode": "subscribe", "hub.verify_token": "tok"},
                                   "tok") is None
    assert whatsapp.verify_webhook(good, "") is None
    assert whatsapp.verify_webhook({}, "tok") is None


# ---------------------------------------------------------------------------
# valid_signature (pure)
# ---------------------------------------------------------------------------


def test_valid_signature_accepts_correct_hmac():
    body = b'{"entry": []}'
    assert whatsapp.valid_signature(SECRET, body, _sig(body)) is True


def test_valid_signature_accepts_uppercase_hex():
    body = b"abc"
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest().upper()
    assert whatsapp.valid_signature(SECRET, body, f"sha256={digest}") is True


def test_valid_signature_rejects_tampering_and_garbage():
    body = b'{"entry": []}'
    good = _sig(body)
    assert whatsapp.valid_signature(SECRET, b'{"entry": [1]}', good) is False  # body swap
    assert whatsapp.valid_signature(SECRET, body, _sig(body, "other-secret")) is False
    assert whatsapp.valid_signature(SECRET, body, None) is False
    assert whatsapp.valid_signature(SECRET, body, "") is False
    assert whatsapp.valid_signature(SECRET, body, "sha1=" + good[7:]) is False
    assert whatsapp.valid_signature(SECRET, body, "sha256=") is False
    assert whatsapp.valid_signature(SECRET, body, good[7:]) is False  # no prefix
    assert whatsapp.valid_signature("", body, good) is False


# ---------------------------------------------------------------------------
# parse_inbound (pure) — statuses-only, referral, batches, junk
# ---------------------------------------------------------------------------


def test_parse_inbound_message_with_referral():
    events = whatsapp.parse_inbound(_msg_payload(referral=REFERRAL))
    assert len(events) == 1
    ev = events[0]
    assert ev["external_id"] == "wamid.A1"
    assert ev["wa_id"] == WA_ID
    assert ev["ts"] == 1719990000
    assert ev["body_excerpt"] == "hi, I want a consult"
    assert ev["referral"] == REFERRAL
    assert ev["phone_number_id"] == PNID


def test_parse_inbound_statuses_only_yields_nothing():
    assert whatsapp.parse_inbound(STATUSES_ONLY) == []


def test_parse_inbound_missing_referral_and_timestamp():
    events = whatsapp.parse_inbound(_msg_payload(ts=None))
    assert len(events) == 1
    assert events[0]["referral"] is None
    assert events[0]["ts"] is None


def test_parse_inbound_caps_body_excerpt_at_120():
    events = whatsapp.parse_inbound(_msg_payload(body="x" * 500))
    assert events[0]["body_excerpt"] == "x" * 120


def test_parse_inbound_multi_entry_batch_preserves_order():
    batch = {
        "object": "whatsapp_business_account",
        "entry": (
            _msg_payload(msg_id="wamid.B1")["entry"]
            + _msg_payload(msg_id="wamid.B2", body="second")["entry"]
        ),
    }
    events = whatsapp.parse_inbound(batch)
    assert [e["external_id"] for e in events] == ["wamid.B1", "wamid.B2"]


def test_parse_inbound_tolerates_malformed_fragments():
    junk = {
        "entry": [
            "not-a-dict",
            {"changes": "nope"},
            {"changes": [None, {"value": []}, {"value": {"messages": "x"}}]},
            {"changes": [{"value": {"messages": [
                42,
                {"id": "wamid.NOFROM"},            # missing from -> dropped
                {"from": WA_ID},                   # missing id -> dropped
                {"id": "wamid.OK", "from": WA_ID, "text": "not-a-dict",
                 "timestamp": "garbage", "referral": "not-a-dict"},
            ]}}]},
        ],
    }
    events = whatsapp.parse_inbound(junk)
    assert len(events) == 1
    ev = events[0]
    assert ev["external_id"] == "wamid.OK"
    assert ev["body_excerpt"] == ""
    assert ev["ts"] is None
    assert ev["referral"] is None
    assert ev["phone_number_id"] is None
    assert whatsapp.parse_inbound({}) == []
    assert whatsapp.parse_inbound({"entry": "x"}) == []


def test_contact_ref_is_sha256_hex_of_wa_id():
    ref = whatsapp.contact_ref(WA_ID)
    assert ref == hashlib.sha256(WA_ID.encode()).hexdigest()
    assert WA_ID not in ref


# ---------------------------------------------------------------------------
# WabaClient (MockTransport, no network)
# ---------------------------------------------------------------------------


def _waba(handler) -> whatsapp.WabaClient:
    return whatsapp.WabaClient(
        token="tok", phone_number_id=PNID,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_waba_client_requires_credentials(monkeypatch):
    monkeypatch.delenv("WABA_TOKEN", raising=False)
    monkeypatch.delenv("WABA_PHONE_NUMBER_ID", raising=False)
    with pytest.raises(whatsapp.WabaError):
        whatsapp.WabaClient()


def test_send_text_payload_url_and_auth():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})

    out = _waba(handler).send_text("971555000999", "your weekly card")
    assert out["messages"][0]["id"] == "wamid.OUT"
    assert seen["url"] == f"https://graph.facebook.com/v20.0/{PNID}/messages"
    assert seen["auth"] == "Bearer tok"
    assert seen["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "971555000999",
        "type": "text",
        "text": {"body": "your weekly card"},
    }


def test_send_template_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.T"}]})

    comps = [{"type": "body", "parameters": [{"type": "text", "text": "7"}]}]
    _waba(handler).send_template("971555000999", "lead_card", lang="en_US", components=comps)
    assert seen["json"]["type"] == "template"
    assert seen["json"]["template"] == {
        "name": "lead_card", "language": {"code": "en_US"}, "components": comps,
    }


def test_waba_retries_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(whatsapp, "_sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"messages": [{"id": "wamid.R"}]})

    out = _waba(handler).send_text("9715", "hi")
    assert out["messages"][0]["id"] == "wamid.R"
    assert calls["n"] == 3


def test_waba_auth_failure_not_retried(monkeypatch):
    monkeypatch.setattr(whatsapp, "_sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"message": "token expired"}})

    with pytest.raises(whatsapp.WabaError) as exc:
        _waba(handler).send_text("9715", "hi")
    assert exc.value.retryable is False
    assert calls["n"] == 1


def test_waba_429_exhausts_as_retryable(monkeypatch):
    monkeypatch.setattr(whatsapp, "_sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": "rate limited"})

    with pytest.raises(whatsapp.WabaError) as exc:
        _waba(handler).send_text("9715", "hi")
    assert exc.value.retryable is True
    assert calls["n"] == whatsapp.MAX_RETRIES + 1


# ---------------------------------------------------------------------------
# API routes — handshake + signature gates (no DB on these paths)
# ---------------------------------------------------------------------------


def test_webhook_get_404_when_verify_token_env_unset(monkeypatch):
    monkeypatch.delenv("WABA_VERIFY_TOKEN", raising=False)
    r = client.get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1",
    })
    assert r.status_code == 404


def test_webhook_get_handshake(monkeypatch):
    monkeypatch.setenv("WABA_VERIFY_TOKEN", "vt")
    r = client.get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "424242",
    })
    assert r.status_code == 200
    assert r.text == "424242"
    bad = client.get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "424242",
    })
    assert bad.status_code == 403


def test_webhook_post_404_when_app_secret_env_unset(monkeypatch):
    """Missing WABA_APP_SECRET must read as no-surface, never open acceptance."""
    monkeypatch.delenv("WABA_APP_SECRET", raising=False)
    monkeypatch.setattr(api, "_connect", _PoisonConn())
    body = json.dumps(_msg_payload()).encode()
    r = client.post("/webhooks/whatsapp", content=body,
                    headers={"X-Hub-Signature-256": _sig(body)})
    assert r.status_code == 404


def test_webhook_post_403_on_bad_or_missing_signature(monkeypatch):
    monkeypatch.setenv("WABA_APP_SECRET", SECRET)
    monkeypatch.setattr(api, "_connect", _PoisonConn())
    body = json.dumps(_msg_payload()).encode()
    assert client.post("/webhooks/whatsapp", content=body).status_code == 403
    r = client.post("/webhooks/whatsapp", content=body,
                    headers={"X-Hub-Signature-256": _sig(body, "wrong-secret")})
    assert r.status_code == 403
    # signature over DIFFERENT bytes (re-serialization attack) is rejected too
    r = client.post("/webhooks/whatsapp", content=body + b" ",
                    headers={"X-Hub-Signature-256": _sig(body)})
    assert r.status_code == 403


def test_webhook_post_statuses_only_200_without_db(monkeypatch):
    monkeypatch.setenv("WABA_APP_SECRET", SECRET)
    monkeypatch.setattr(api, "_connect", _PoisonConn())
    body = json.dumps(STATUSES_ONLY).encode()
    r = client.post("/webhooks/whatsapp", content=body,
                    headers={"X-Hub-Signature-256": _sig(body)})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_webhook_post_200_even_when_processing_blows_up(monkeypatch):
    """Meta retries on non-2xx: processing errors are logged, never bounced."""
    monkeypatch.setenv("WABA_APP_SECRET", SECRET)

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(api, "_connect", _boom)
    body = json.dumps(_msg_payload()).encode()
    r = client.post("/webhooks/whatsapp", content=body,
                    headers={"X-Hub-Signature-256": _sig(body)})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_webhook_post_200_on_signed_garbage_json(monkeypatch):
    monkeypatch.setenv("WABA_APP_SECRET", SECRET)
    monkeypatch.setattr(api, "_connect", _PoisonConn())
    for body in (b"not json at all", b'["a", "list"]'):
        r = client.post("/webhooks/whatsapp", content=body,
                        headers={"X-Hub-Signature-256": _sig(body)})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# DB-backed: end-to-end insert, replay dedupe, privacy invariants
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    db.run_migrations()
    # SHIM: 003's connections.kind check predates Phase D and doesn't allow
    # 'whatsapp' yet; migration 007 doesn't extend it (flagged to integrator).
    with db.connect(autocommit=True) as conn:
        conn.execute("alter table connections drop constraint if exists connections_kind_check")
        conn.execute(
            "alter table connections add constraint connections_kind_check check"
            " (kind in ('gsc','wordpress','ga4','google_ads','meta_ads','whatsapp'))"
        )


@pytest.fixture()
def seeded(_migrated):
    from psycopg.types.json import Jsonb

    tag = uuid.uuid4().hex[:10]
    pnid = f"pn{tag}"
    with db.connect(autocommit=True) as conn:
        org_id = conn.execute(
            "insert into orgs (name) values (%s) returning id", (f"wa-{tag}",)
        ).fetchone()["id"]
        site_id = conn.execute(
            "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
            (org_id, f"{tag}.example.com"),
        ).fetchone()["id"]
        conn.execute(
            "insert into connections (org_id, site_id, kind, meta)"
            " values (%s, %s, 'whatsapp', %s)",
            (org_id, site_id, Jsonb({"phone_number_id": pnid, "recipient_wa_id_ref": "opt"})),
        )
    return {"org": str(org_id), "site": str(site_id), "pnid": pnid}


def _fetch_lead(external_id: str):
    with db.connect(autocommit=True) as conn:
        return conn.execute(
            "select * from booked_leads where external_id = %s", (external_id,)
        ).fetchall()


@needs_db
def test_inbound_end_to_end_replay_dedupe_and_privacy(seeded, monkeypatch):
    monkeypatch.setenv("WABA_APP_SECRET", SECRET)
    msg_id = f"wamid.E2E-{seeded['pnid']}"
    payload = _msg_payload(msg_id=msg_id, pnid=seeded["pnid"],
                           referral=REFERRAL, body="q" * 300)
    body = json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": _sig(body)}

    assert client.post("/webhooks/whatsapp", content=body, headers=headers).status_code == 200
    # Meta-style replay: same payload again -> deduped on external_id
    assert client.post("/webhooks/whatsapp", content=body, headers=headers).status_code == 200

    rows = _fetch_lead(msg_id)
    assert len(rows) == 1
    row = rows[0]
    assert str(row["org_id"]) == seeded["org"]
    assert str(row["site_id"]) == seeded["site"]
    assert row["source"] == "whatsapp"
    assert row["contact_ref"] == hashlib.sha256(WA_ID.encode()).hexdigest()
    assert row["attribution"]["referral"] == REFERRAL
    assert row["attribution"]["body_excerpt"] == "q" * 120
    assert row["occurred_at"].timestamp() == 1719990000
    # privacy law: the raw phone number appears NOWHERE in the stored row
    assert WA_ID not in repr(row)


@needs_db
def test_inbound_unknown_phone_number_dropped(seeded, monkeypatch):
    monkeypatch.setenv("WABA_APP_SECRET", SECRET)
    msg_id = f"wamid.UNKNOWN-{seeded['pnid']}"
    payload = _msg_payload(msg_id=msg_id, pnid="no-such-pnid")
    body = json.dumps(payload).encode()
    r = client.post("/webhooks/whatsapp", content=body,
                    headers={"X-Hub-Signature-256": _sig(body)})
    assert r.status_code == 200
    assert _fetch_lead(msg_id) == []


@needs_db
def test_record_inbound_leads_returns_insert_count(seeded):
    events = whatsapp.parse_inbound(
        _msg_payload(msg_id=f"wamid.CNT-{seeded['pnid']}", pnid=seeded["pnid"])
    )
    with db.connect() as conn:
        assert whatsapp.record_inbound_leads(conn, events) == 1
        assert whatsapp.record_inbound_leads(conn, events) == 0  # replay
