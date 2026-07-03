"""Vault + GSC client tests.

ZERO network: all GSC HTTP goes through httpx.MockTransport and google-auth
credentials are replaced by a fake (token attribute + no-op refresh) — real
Credentials objects are never constructed or refreshed. DB-backed tests
(store/load/mark against the connections table) skip when DATABASE_URL is unset.
"""

from __future__ import annotations

import json
import os
import uuid

import httpx
import pytest

from gm.connections import gsc as gsc_mod
from gm.connections import vault
from gm.connections.gsc import GscAuthError, GscClient, GscError

# --- vault: pure crypto paths (no DB) -----------------------------------------------


@pytest.fixture()
def keypair(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    public_b64, private_b64 = vault.generate_keypair()
    monkeypatch.setenv(vault.PUBLIC_KEY_ENV, public_b64)
    monkeypatch.setenv(vault.PRIVATE_KEY_ENV, private_b64)
    return public_b64, private_b64


def test_vault_roundtrip(keypair):
    payload = {"client_email": "svc@example.iam", "private_key": "-----BEGIN...", "n": 3}
    blob = vault.seal(payload)
    assert isinstance(blob, bytes)
    assert json.dumps(payload).encode() not in blob  # actually encrypted
    assert vault.open_sealed(blob) == payload


def test_seal_works_without_private_key(keypair, monkeypatch: pytest.MonkeyPatch):
    """Fetcher/API processes hold only the public key: sealing must still work."""
    monkeypatch.delenv(vault.PRIVATE_KEY_ENV, raising=False)
    blob = vault.seal({"token": "t"})
    assert isinstance(blob, bytes) and len(blob) > 0
    with pytest.raises(vault.VaultLocked):
        vault.open_sealed(blob)


def test_seal_without_public_key_is_locked(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(vault.PUBLIC_KEY_ENV, raising=False)
    with pytest.raises(vault.VaultLocked):
        vault.seal({"token": "t"})


def test_open_sealed_rejects_unknown_key_version(keypair):
    blob = vault.seal({"token": "t"})
    with pytest.raises(ValueError):
        vault.open_sealed(blob, key_version=2)


def test_keypairs_are_distinct_and_garbage_key_is_locked(monkeypatch: pytest.MonkeyPatch):
    assert vault.generate_keypair() != vault.generate_keypair()
    monkeypatch.setenv(vault.PUBLIC_KEY_ENV, "not-base64!!")
    with pytest.raises(vault.VaultLocked):
        vault.seal({"token": "t"})


# --- vault: connections table (DB) ---------------------------------------------------

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


@pytest.fixture()
def db_site(keypair):
    from gm import db

    db.run_migrations()
    with db.connect(autocommit=True) as conn:
        conn.execute("delete from connections")
        org = conn.execute(
            "insert into orgs (name) values ('vault-test') returning id"
        ).fetchone()
        site = conn.execute(
            "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
            (org["id"], f"vault-test-{uuid.uuid4().hex[:8]}.example"),
        ).fetchone()
        yield conn, org["id"], site["id"]
        conn.execute("delete from connections where site_id = %s", (site["id"],))
        conn.execute("delete from sites where id = %s", (site["id"],))
        conn.execute("delete from orgs where id = %s", (org["id"],))


@requires_db
def test_store_load_mark_connection(db_site, monkeypatch: pytest.MonkeyPatch):
    conn, org_id, site_id = db_site
    creds = {"client_email": "svc@example.iam", "private_key": "pem"}
    meta = {"property": "sc-domain:example.com"}

    conn_id = vault.store_connection(
        conn, org_id=org_id, site_id=site_id, kind="gsc", credentials=creds, meta=meta
    )
    assert uuid.UUID(conn_id)

    row = vault.load_connection(conn, site_id, "gsc")
    assert row["id"] == uuid.UUID(conn_id)
    assert row["credentials"] == creds
    assert row["meta"] == meta
    assert row["status"] == "ok"
    assert row["key_version"] == 1

    # Re-store upserts the same (site_id, kind) row.
    conn_id2 = vault.store_connection(
        conn, org_id=org_id, site_id=site_id, kind="gsc",
        credentials={"rotated": True}, meta=meta,
    )
    assert conn_id2 == conn_id
    assert vault.load_connection(conn, site_id, "gsc")["credentials"] == {"rotated": True}

    vault.mark_connection(conn, conn_id, ok=False, error="HTTP 403")
    row = vault.load_connection(conn, site_id, "gsc")
    assert row["status"] == "broken"
    assert row["last_error"] == "HTTP 403"

    vault.mark_connection(conn, conn_id, ok=True)
    row = vault.load_connection(conn, site_id, "gsc")
    assert row["status"] == "ok"
    assert row["last_error"] is None
    assert row["last_ok_at"] is not None

    with pytest.raises(LookupError):
        vault.load_connection(conn, site_id, "wordpress")


@requires_db
def test_load_connection_without_private_key(db_site, monkeypatch: pytest.MonkeyPatch):
    conn, org_id, site_id = db_site
    vault.store_connection(
        conn, org_id=org_id, site_id=site_id, kind="gsc",
        credentials={"k": "v"}, meta={},
    )
    monkeypatch.delenv(vault.PRIVATE_KEY_ENV, raising=False)
    with pytest.raises(vault.VaultLocked):
        vault.load_connection(conn, site_id, "gsc")


# --- GSC client (MockTransport, fake credentials) ------------------------------------


class FakeCredentials:
    """Stands in for google-auth Credentials: token attribute + no-op refresh."""

    def __init__(self, valid: bool = True):
        self.token = "tok-initial"
        self.valid = valid
        self.refresh_calls = 0

    def refresh(self, request) -> None:
        self.refresh_calls += 1
        self.token = "tok-refreshed"
        self.valid = True


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gsc_mod, "_sleep", lambda _s: None)


def make_client(responses: list[tuple[int, object]]) -> tuple[httpx.Client, list[httpx.Request]]:
    """MockTransport client replaying `responses` in order (last one repeats)."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status, body = responses[min(len(requests) - 1, len(responses) - 1)]
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


def make_gsc(
    responses: list[tuple[int, object]],
    property_url: str = "sc-domain:example.com",
    credentials: FakeCredentials | None = None,
) -> tuple[GscClient, list[httpx.Request]]:
    client, requests = make_client(responses)
    gsc = GscClient(
        service_account_info={},  # unused: credentials override wins
        property_url=property_url,
        client=client,
        credentials=credentials or FakeCredentials(),
    )
    return gsc, requests


ROWS_PAGE = {
    "rows": [
        {
            "keys": ["https://example.com/a", "best widgets"],
            "clicks": 12,
            "impressions": 340,
            "ctr": 0.0353,
            "position": 7.2,
        },
        {
            "keys": ["https://example.com/b", "widget price"],
            "clicks": 3,
            "impressions": 90,
            "ctr": 0.0333,
            "position": 11.8,
        },
    ]
}


def test_query_parses_rows_and_builds_payload():
    gsc, requests = make_gsc([(200, ROWS_PAGE)])
    rows = gsc.query(
        start_date="2026-06-01", end_date="2026-06-28", dimensions=["page", "query"]
    )
    assert rows == ROWS_PAGE["rows"]
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body == {
        "startDate": "2026-06-01",
        "endDate": "2026-06-28",
        "dimensions": ["page", "query"],
        "rowLimit": 25_000,
        "startRow": 0,
        "type": "web",
        "dataState": "final",
    }
    assert requests[0].headers["authorization"] == "Bearer tok-initial"


def test_query_empty_response_yields_no_rows():
    gsc, _ = make_gsc([(200, {})])
    assert gsc.query(start_date="2026-06-01", end_date="2026-06-02", dimensions=["page"]) == []


def test_sc_domain_property_is_url_encoded():
    gsc, requests = make_gsc([(200, {})], property_url="sc-domain:example.com")
    gsc.query(start_date="2026-06-01", end_date="2026-06-02", dimensions=["page"])
    assert b"/sites/sc-domain%3Aexample.com/searchAnalytics/query" in requests[0].url.raw_path


def test_url_prefix_property_is_url_encoded():
    gsc, requests = make_gsc([(200, {})], property_url="https://example.com/")
    gsc.query(start_date="2026-06-01", end_date="2026-06-02", dimensions=["page"])
    assert b"/sites/https%3A%2F%2Fexample.com%2F/" in requests[0].url.raw_path


def test_query_all_paginates_until_empty():
    page = lambda n: {"rows": [{"keys": [f"p{n}-{i}"], "clicks": i} for i in range(2)]}  # noqa: E731
    gsc, requests = make_gsc([(200, page(1)), (200, page(2)), (200, {"rows": []})])
    pages = list(
        gsc.query_all(
            start_date="2026-06-01", end_date="2026-06-28",
            dimensions=["page", "query"], row_limit=2,
        )
    )
    assert [len(p) for p in pages] == [2, 2]
    start_rows = [json.loads(r.content)["startRow"] for r in requests]
    assert start_rows == [0, 2, 4]


def test_auth_error_on_403_and_401():
    for status in (401, 403):
        gsc, requests = make_gsc([(status, {"error": {"message": "denied"}})])
        with pytest.raises(GscAuthError):
            gsc.query(start_date="2026-06-01", end_date="2026-06-02", dimensions=["page"])
        assert len(requests) == 1  # auth failures are not retried


def test_retries_on_429_and_5xx_then_succeeds():
    gsc, requests = make_gsc([(429, "slow down"), (503, "unavailable"), (200, ROWS_PAGE)])
    rows = gsc.query(start_date="2026-06-01", end_date="2026-06-02", dimensions=["page"])
    assert len(rows) == 2
    assert len(requests) == 3


def test_retries_exhaust_to_retryable_error():
    gsc, requests = make_gsc([(500, "boom")])
    with pytest.raises(GscError) as excinfo:
        gsc.query(start_date="2026-06-01", end_date="2026-06-02", dimensions=["page"])
    assert not isinstance(excinfo.value, GscAuthError)
    assert excinfo.value.retryable
    assert len(requests) == 1 + gsc_mod.MAX_RETRIES


def test_invalid_credentials_are_refreshed_without_network():
    creds = FakeCredentials(valid=False)
    gsc, requests = make_gsc([(200, {"siteEntry": [{"siteUrl": "sc-domain:example.com"}]})],
                             credentials=creds)
    sites = gsc.list_sites()
    assert sites == [{"siteUrl": "sc-domain:example.com"}]
    assert creds.refresh_calls == 1
    assert requests[0].headers["authorization"] == "Bearer tok-refreshed"
