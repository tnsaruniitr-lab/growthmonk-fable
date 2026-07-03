"""WordPress publish + post-publish verify tests (Phase C wave-3).

ZERO network: WordPress REST goes through httpx.MockTransport; BEV probes use
fake fetchers; GSC inspect_url uses MockTransport + fake credentials; DNS goes
through a monkeypatched gm.audit.safety._getaddrinfo. DB-backed flows skip
when DATABASE_URL is unset.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import socket
import uuid
from types import SimpleNamespace

import httpx
import pytest

from gm.audit import safety
from gm.audit.bev import NOT_FOUND_PATH
from gm.audit.fetch import FetchResult
from gm.connections import vault
from gm.connections.gsc import GscClient
from gm.delivery import wordpress as wp_mod
from gm.delivery.verify import verify_publish
from gm.delivery.wordpress import (
    JSONLD_MARKER,
    WpClient,
    WpError,
    connect_wordpress,
    handle_publish,
    indexnow_ping,
    publish_content_item,
)

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(wp_mod, "_sleep", lambda _s: None)


# ---------------------------------------------------------------------------
# Fake WordPress server (MockTransport handler with canned routes)
# ---------------------------------------------------------------------------

ME_JSON = {
    "id": 7,
    "name": "gm-bot",
    "roles": ["editor"],
    "capabilities": {"edit_posts": True, "publish_posts": True},
}


class FakeWpServer:
    def __init__(self, *, roles=("editor",), strip_jsonld=False, auth_fail=None,
                 link="https://Blog.Example.com/Hello-World/"):
        self.roles = list(roles)
        self.strip_jsonld = strip_jsonld
        self.auth_fail = auth_fail  # (status, json_body) applied to every route
        self.link = link
        self.requests: list[httpx.Request] = []
        self.created: list[dict] = []
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.auth_fail is not None:
            status, body = self.auth_fail
            return httpx.Response(status, json=body)
        path = request.url.path
        if path.endswith("/wp/v2/users/me"):
            me = dict(ME_JSON, roles=self.roles)
            return httpx.Response(200, json=me)
        if path.endswith("/wp/v2/posts") and request.method == "POST":
            body = json.loads(request.content)
            self.created.append(body)
            content = body.get("content", "")
            if self.strip_jsonld:
                content = re.sub(
                    r'<script type="application/ld\+json">.*?</script>\n?',
                    "", content, flags=re.DOTALL,
                )
            return httpx.Response(201, json={
                "id": 100 + len(self.created),
                "link": self.link,
                "status": body.get("status"),
                "content": {"raw": content, "rendered": content},
            })
        if request.method == "DELETE" and "/wp/v2/posts/" in path:
            self.deleted.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(404, json={"code": "rest_no_route", "message": "no route"})


def make_wp(server: FakeWpServer) -> WpClient:
    client = httpx.Client(transport=httpx.MockTransport(server.handler))
    return WpClient("https://blog.example.com", "gm-bot", "abcd efgh", client=client)


def make_seq_wp(responses: list[tuple[int, object]]) -> tuple[WpClient, list[httpx.Request]]:
    """WpClient replaying `responses` in order (last one repeats)."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        status, body = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return WpClient("https://blog.example.com", "u", "p", client=client), calls


# ---------------------------------------------------------------------------
# WpClient: transport rules
# ---------------------------------------------------------------------------

def test_https_only_refuses_http_base_url():
    with pytest.raises(WpError) as excinfo:
        WpClient("http://blog.example.com", "u", "p")
    assert "https" in str(excinfo.value)


def test_basic_auth_header_is_sent():
    server = FakeWpServer()
    make_wp(server).me()
    expected = "Basic " + base64.b64encode(b"gm-bot:abcd efgh").decode("ascii")
    assert server.requests[0].headers["authorization"] == expected


def test_retries_5xx_then_succeeds():
    wp, calls = make_seq_wp([(503, "unavailable"), (200, ME_JSON)])
    assert wp.me()["name"] == "gm-bot"
    assert len(calls) == 2


def test_retries_exhaust_to_retryable_error():
    wp, calls = make_seq_wp([(500, "boom")])
    with pytest.raises(WpError) as excinfo:
        wp.me()
    assert excinfo.value.retryable
    assert len(calls) == 1 + wp_mod.MAX_RETRIES


def test_401_is_not_retried_and_classified_generic():
    wp, calls = make_seq_wp([(401, {"code": "rest_not_logged_in", "message": "nope"})])
    with pytest.raises(WpError) as excinfo:
        wp.me()
    assert not excinfo.value.retryable
    assert len(calls) == 1
    assert "check the username" in str(excinfo.value)
    assert excinfo.value.code == "rest_not_logged_in"


# ---------------------------------------------------------------------------
# Preflight paths
# ---------------------------------------------------------------------------

def test_preflight_happy_path_creates_and_deletes_private_draft():
    server = FakeWpServer()
    report = make_wp(server).preflight()
    assert report["ok"] is True
    assert report["role"] == "editor"
    assert report["warnings"] == [] and report["errors"] == []
    # The test draft is PRIVATE, carries JSON-LD, and gets force-deleted.
    assert server.created[0]["status"] == "private"
    assert JSONLD_MARKER in server.created[0]["content"]
    assert server.deleted == ["101"]
    delete_req = server.requests[-1]
    assert delete_req.url.params.get("force") == "true"


def test_preflight_warns_on_administrator_role():
    server = FakeWpServer(roles=("administrator",))
    report = make_wp(server).preflight()
    assert report["ok"] is True
    assert report["role"] == "administrator"
    assert any("administrator" in w and "least-privilege" in w for w in report["warnings"])


def test_preflight_classifies_app_passwords_disabled_401():
    server = FakeWpServer(auth_fail=(401, {
        "code": "application_passwords_disabled",
        "message": "Application passwords are not available.",
    }))
    report = make_wp(server).preflight()
    assert report["ok"] is False
    assert any("Application Passwords are disabled" in e for e in report["errors"])
    assert len(server.requests) == 1  # auth failures are not retried


def test_preflight_detects_kses_stripping_via_round_trip():
    server = FakeWpServer(strip_jsonld=True)
    report = make_wp(server).preflight()
    assert report["ok"] is True  # a warning, not a blocker
    assert any("kses stripped the JSON-LD" in w for w in report["warnings"])


# ---------------------------------------------------------------------------
# publish_draft
# ---------------------------------------------------------------------------

def test_publish_draft_prepends_jsonld_and_returns_id_link_status():
    server = FakeWpServer()
    result = make_wp(server).publish_draft(
        title="Hello", content_html="<p>Body</p>", excerpt="sum",
        meta_jsonld={"@type": "Article", "headline": "Hello"},
    )
    body = server.created[0]
    assert body["status"] == "draft"
    assert body["excerpt"] == "sum"
    assert body["content"].startswith(JSONLD_MARKER)
    assert '"@type": "Article"' in body["content"]
    assert body["content"].endswith("<p>Body</p>")
    assert result == {"id": 101, "link": server.link, "status": "draft"}


def test_publish_draft_notes_kses_stripping():
    server = FakeWpServer(strip_jsonld=True)
    result = make_wp(server).publish_draft(
        title="Hello", content_html="<p>Body</p>", meta_jsonld={"@type": "Article"},
    )
    assert result["jsonld_stripped"] is True
    assert "kses" in result["note"]


def test_publish_draft_without_jsonld_has_no_script_block():
    server = FakeWpServer()
    result = make_wp(server).publish_draft(title="Hello", content_html="<p>Body</p>")
    assert JSONLD_MARKER not in server.created[0]["content"]
    assert "jsonld_stripped" not in result


# ---------------------------------------------------------------------------
# IndexNow (fire-and-forget)
# ---------------------------------------------------------------------------

def test_indexnow_skipped_with_note_when_key_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("INDEXNOW_KEY", raising=False)
    out = indexnow_ping("https://blog.example.com/hello-world/")
    assert out["sent"] is False
    assert "INDEXNOW_KEY unset" in out["note"]


def test_indexnow_posts_host_key_urllist(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("INDEXNOW_KEY", "k1")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = indexnow_ping("https://blog.example.com/hello-world/", client=client)
    assert out == {"sent": True, "status": 200}
    assert str(captured[0].url) == wp_mod.INDEXNOW_ENDPOINT
    assert json.loads(captured[0].content) == {
        "host": "blog.example.com",
        "key": "k1",
        "urlList": ["https://blog.example.com/hello-world/"],
    }


def test_indexnow_swallows_transport_errors(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("INDEXNOW_KEY", "k1")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = indexnow_ping("https://blog.example.com/x", client=client)
    assert out["sent"] is False
    assert "IndexNow ping failed" in out["note"]


# ---------------------------------------------------------------------------
# GSC inspect_url (the one appended method)
# ---------------------------------------------------------------------------

class FakeCredentials:
    def __init__(self):
        self.token = "tok"
        self.valid = True

    def refresh(self, request) -> None:  # pragma: no cover - valid=True skips it
        self.valid = True


def test_inspect_url_posts_and_parses():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={
            "inspectionResult": {
                "inspectionResultLink": "https://search.google.com/search-console/inspect?x=1",
                "indexStatusResult": {
                    "verdict": "PASS",
                    "coverageState": "Submitted and indexed",
                    "robotsTxtState": "ALLOWED",
                    "indexingState": "INDEXING_ALLOWED",
                    "lastCrawlTime": "2026-07-01T03:04:05Z",
                },
            }
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gsc = GscClient({}, "sc-domain:example.com", client=client, credentials=FakeCredentials())
    out = gsc.inspect_url("https://blog.example.com/hello-world/")
    assert str(captured[0].url) == (
        "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
    )
    assert json.loads(captured[0].content) == {
        "inspectionUrl": "https://blog.example.com/hello-world/",
        "siteUrl": "sc-domain:example.com",
    }
    assert out == {
        "verdict": "PASS",
        "coverage_state": "Submitted and indexed",
        "indexing_state": "INDEXING_ALLOWED",
        "robots_txt_state": "ALLOWED",
        "last_crawl_time": "2026-07-01T03:04:05Z",
        "inspection_link": "https://search.google.com/search-console/inspect?x=1",
    }


def test_inspect_url_tolerates_missing_index_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"inspectionResult": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gsc = GscClient({}, "sc-domain:example.com", client=client, credentials=FakeCredentials())
    out = gsc.inspect_url("https://blog.example.com/x")
    assert out["verdict"] is None and out["inspection_link"] is None


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------

PACKAGE = {
    "article": {"title": "Hello World", "html": "<p>Body</p>"},
    "jsonld": {"@context": "https://schema.org", "@type": "Article", "headline": "Hello"},
    "meta": {"description": "Hi"},
}


@pytest.fixture()
def keypair(monkeypatch: pytest.MonkeyPatch):
    public_b64, private_b64 = vault.generate_keypair()
    monkeypatch.setenv(vault.PUBLIC_KEY_ENV, public_b64)
    monkeypatch.setenv(vault.PRIVATE_KEY_ENV, private_b64)


@pytest.fixture()
def db_env():
    from psycopg.types.json import Jsonb

    from gm import db

    db.run_migrations()
    with db.connect(autocommit=True) as conn:
        org = conn.execute(
            "insert into orgs (name) values ('wp-test') returning id"
        ).fetchone()["id"]
        site = conn.execute(
            "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
            (org, f"wp-test-{uuid.uuid4().hex[:8]}.example"),
        ).fetchone()["id"]
        item = conn.execute(
            "insert into content_items (org_id, site_id, kind, status)"
            " values (%s, %s, 'new', 'review') returning id",
            (org, site),
        ).fetchone()["id"]
        conn.execute(
            "insert into drafts (org_id, content_item_id, version, package)"
            " values (%s, %s, 1, %s)",
            (org, item, Jsonb(PACKAGE)),
        )
        yield conn, org, site, item
        conn.execute("delete from verify_events where content_item_id = %s", (item,))
        conn.execute("delete from publish_events where content_item_id = %s", (item,))
        conn.execute("delete from drafts where content_item_id = %s", (item,))
        conn.execute("delete from content_items where id = %s", (item,))
        conn.execute("delete from pages where site_id = %s", (site,))
        conn.execute("delete from connections where site_id = %s", (site,))
        conn.execute("delete from jobs where site_id = %s", (site,))
        conn.execute("delete from sites where id = %s", (site,))
        conn.execute("delete from orgs where id = %s", (org,))


# ---------------------------------------------------------------------------
# connect_wordpress (vault store)
# ---------------------------------------------------------------------------

@requires_db
def test_connect_wordpress_stores_connection_on_preflight_ok(db_env, keypair):
    conn, org, site, _item = db_env
    server = FakeWpServer()
    report = connect_wordpress(
        conn, org_id=org, site_id=site, base_url="https://blog.example.com",
        username="gm-bot", app_password="abcd efgh", wp=make_wp(server),
    )
    assert report["ok"] is True and report["stored"] is True
    row = vault.load_connection(conn, site, "wordpress")
    assert row["credentials"]["base_url"] == "https://blog.example.com"
    assert row["credentials"]["app_password"] == "abcd efgh"
    assert row["meta"]["preflight"]["role"] == "editor"


@requires_db
def test_connect_wordpress_does_not_store_on_preflight_failure(db_env, keypair):
    conn, org, site, _item = db_env
    server = FakeWpServer(auth_fail=(401, {"code": "application_passwords_disabled",
                                           "message": "unavailable"}))
    report = connect_wordpress(
        conn, org_id=org, site_id=site, base_url="https://blog.example.com",
        username="gm-bot", app_password="bad", wp=make_wp(server),
    )
    assert report["ok"] is False and report["stored"] is False
    with pytest.raises(LookupError):
        vault.load_connection(conn, site, "wordpress")


# ---------------------------------------------------------------------------
# handle_publish / publish_content_item flow
# ---------------------------------------------------------------------------

@requires_db
def test_publish_flow_events_pages_status_and_verify_jobs(
    db_env, monkeypatch: pytest.MonkeyPatch
):
    conn, org, site, item = db_env
    monkeypatch.delenv("INDEXNOW_KEY", raising=False)
    server = FakeWpServer(link="https://Blog.Example.com/Hello-World/")

    result = publish_content_item(conn, content_item_id=item, wp=make_wp(server))

    # WP got the draft with JSON-LD prepended and the meta description excerpt.
    assert server.created[0]["status"] == "draft"
    assert server.created[0]["content"].startswith(JSONLD_MARKER)
    assert server.created[0]["excerpt"] == "Hi"

    pe = conn.execute(
        "select * from publish_events where content_item_id = %s", (item,)
    ).fetchall()
    assert len(pe) == 1
    assert pe[0]["target"] == "wordpress"
    assert pe[0]["external_id"] == "101"
    assert pe[0]["url"] == "https://Blog.Example.com/Hello-World/"

    # Pages upsert used canonicalize_url (host lowercased, path verbatim).
    page = conn.execute("select * from pages where site_id = %s", (site,)).fetchone()
    assert page["url_norm"] == "https://blog.example.com/Hello-World/"
    ci = conn.execute("select * from content_items where id = %s", (item,)).fetchone()
    assert ci["status"] == "published"
    assert ci["page_id"] == page["id"]

    # IndexNow honestly skipped without a key.
    assert result["indexnow"]["sent"] is False
    assert "INDEXNOW_KEY unset" in result["indexnow"]["note"]

    # Verify jobs at ~T+15m and ~T+72h with idempotency keys.
    jobs = conn.execute(
        "select * from jobs where type = 'verify_publish' and site_id = %s"
        " order by run_after",
        (site,),
    ).fetchall()
    assert [j["idempotency_key"] for j in jobs] == [
        f"verify_publish:{item}:early", f"verify_publish:{item}:late",
    ]
    assert [j["payload"]["attempt"] for j in jobs] == ["early", "late"]
    now = dt.datetime.now(dt.UTC)
    assert dt.timedelta(minutes=14) < (jobs[0]["run_after"] - now) < dt.timedelta(minutes=16)
    assert dt.timedelta(hours=71) < (jobs[1]["run_after"] - now) < dt.timedelta(hours=73)

    # Re-publish: verify jobs dedupe on idempotency_key; a second event is recorded.
    result2 = publish_content_item(conn, content_item_id=item, wp=make_wp(server))
    assert result2["verify_job_ids"] == [None, None]
    n_jobs = conn.execute(
        "select count(*) as n from jobs where type = 'verify_publish' and site_id = %s",
        (site,),
    ).fetchone()["n"]
    assert n_jobs == 2


@requires_db
def test_handle_publish_uses_vault_client_and_records_ping_note(
    db_env, monkeypatch: pytest.MonkeyPatch
):
    from gm.infra.jobs import enqueue

    conn, org, site, item = db_env
    monkeypatch.delenv("INDEXNOW_KEY", raising=False)
    server = FakeWpServer()
    monkeypatch.setattr(wp_mod, "client_from_connection", lambda _c, _s: make_wp(server))

    job_id = enqueue(conn, type="publish", org_id=org, site_id=site,
                     payload={"content_item_id": str(item)})
    ctx = SimpleNamespace(
        job=SimpleNamespace(id=job_id, org_id=org, site_id=site,
                            payload={"content_item_id": str(item)}),
        conn=conn,
    )
    handle_publish(ctx)

    job = conn.execute("select payload from jobs where id = %s", (job_id,)).fetchone()
    assert job["payload"]["indexnow"]["sent"] is False
    assert "publish_event_id" in job["payload"]
    assert conn.execute(
        "select status from content_items where id = %s", (item,)
    ).fetchone()["status"] == "published"


@requires_db
def test_handle_publish_requires_content_item_id(db_env):
    conn, _org, _site, _item = db_env
    ctx = SimpleNamespace(job=SimpleNamespace(id=1, payload={}), conn=conn)
    with pytest.raises(ValueError):
        handle_publish(ctx)


# ---------------------------------------------------------------------------
# verify_publish — fake fetchers, early/late transitions
# ---------------------------------------------------------------------------

PUBLISH_URL = "https://blog.example.com/hello-world/"
WORDS = " ".join(f"word{i}" for i in range(600))
GOOD_HTML = (
    "<html><head><title>Hello</title>"
    '<script type="application/ld+json">'
    '{"@context":"https://schema.org","@type":"Article","headline":"Hello",'
    '"@id":"https://blog.example.com/hello-world/#article"}'
    "</script></head>"
    f"<body><h1>Hello World</h1><p>{WORDS}</p></body></html>"
)
NO_SCHEMA_HTML = (
    "<html><head><title>Hello</title></head>"
    f"<body><h1>Hello World</h1><p>{WORDS}</p></body></html>"
)
NF_HTML = (
    "<html><head><title>404</title></head>"
    "<body><h1>Not found</h1><p>that page is missing entirely</p></body></html>"
)


def fake_factory(page_html: str = GOOD_HTML, blocked_ua_markers: tuple[str, ...] = ()):
    """BEV fetcher factory: serves `page_html` to every UA, a distinct 404 body
    for the not-found probe, and 403s to UAs matching `blocked_ua_markers`."""

    def factory(ua: str):
        def fetch(url: str) -> FetchResult:
            if url.endswith(NOT_FOUND_PATH):
                return FetchResult(url, url, 404, {}, NF_HTML, 1, [url])
            for marker in blocked_ua_markers:
                if marker in ua:
                    return FetchResult(url, url, 403, {}, "denied", 1, [url])
            return FetchResult(url, url, 200, {}, page_html, 1, [url])

        return fetch

    return factory


@pytest.fixture()
def public_dns(monkeypatch: pytest.MonkeyPatch):
    def resolve(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(safety, "_getaddrinfo", resolve)


def _publish_event(conn, org, item, url: str = PUBLISH_URL) -> None:
    conn.execute(
        "insert into publish_events (org_id, content_item_id, target, external_id, url)"
        " values (%s, %s, 'wordpress', '101', %s)",
        (org, item, url),
    )
    conn.execute(
        "update content_items set status = 'published' where id = %s", (item,)
    )


@requires_db
def test_verify_early_records_events_without_transition(db_env, public_dns):
    conn, org, _site, item = db_env
    _publish_event(conn, org, item)

    res = verify_publish(conn, content_item_id=item, attempt="early",
                         fetcher_factory=fake_factory())

    assert res["passed"] is True
    assert res["bev_ok"] is True and res["bev_classification"] == "fully_accessible"
    assert res["schema_present"] is True
    assert res["inspection"] is None
    assert any("gsc inspection skipped" in n for n in res["notes"])
    # Early attempt draws no verdict.
    status = conn.execute(
        "select status from content_items where id = %s", (item,)
    ).fetchone()["status"]
    assert status == "published"
    kinds = sorted(r["kind"] for r in conn.execute(
        "select kind from verify_events where content_item_id = %s", (item,)
    ).fetchall())
    assert kinds == ["bev", "schema"]


@requires_db
def test_verify_late_pass_transitions_to_verified(db_env, public_dns):
    conn, org, _site, item = db_env
    _publish_event(conn, org, item)

    res = verify_publish(conn, content_item_id=item, attempt="late",
                         fetcher_factory=fake_factory())

    assert res["passed"] is True
    assert conn.execute(
        "select status from content_items where id = %s", (item,)
    ).fetchone()["status"] == "verified"


@requires_db
def test_verify_late_bot_blocked_transitions_to_verify_failed(db_env, public_dns):
    conn, org, _site, item = db_env
    _publish_event(conn, org, item)

    res = verify_publish(
        conn, content_item_id=item, attempt="late",
        fetcher_factory=fake_factory(blocked_ua_markers=("GPTBot",)),
    )

    assert res["passed"] is False and res["bev_ok"] is False
    assert res["bot_status"]["gptbot"] == 403
    assert res["bot_status"]["googlebot"] == 200
    assert conn.execute(
        "select status from content_items where id = %s", (item,)
    ).fetchone()["status"] == "verify_failed"
    # The honest evidence is on record.
    bev_row = conn.execute(
        "select result from verify_events where content_item_id = %s and kind = 'bev'",
        (item,),
    ).fetchone()
    assert bev_row["result"]["ok"] is False
    assert bev_row["result"]["bot_status"]["gptbot"] == 403


@requires_db
def test_verify_missing_schema_fails_honestly(db_env, public_dns):
    conn, org, _site, item = db_env
    _publish_event(conn, org, item)

    res = verify_publish(conn, content_item_id=item, attempt="early",
                         fetcher_factory=fake_factory(page_html=NO_SCHEMA_HTML))

    assert res["bev_ok"] is True  # bots can read it fine...
    assert res["schema_present"] is False  # ...but the served HTML lost its JSON-LD
    assert res["passed"] is False
    assert any("no JSON-LD entities" in n for n in res["notes"])


@requires_db
def test_verify_uses_gsc_inspection_when_connection_exists(db_env, keypair, public_dns):
    conn, org, site, item = db_env
    _publish_event(conn, org, item)
    vault.store_connection(
        conn, org_id=org, site_id=site, kind="gsc",
        credentials={"client_email": "svc@x.iam"},
        meta={"property": "sc-domain:example.com"},
    )
    seen: list[dict] = []

    def gsc_factory(row: dict):
        seen.append(row)
        return SimpleNamespace(inspect_url=lambda url: {
            "verdict": "PASS", "coverage_state": "Submitted and indexed",
        })

    res = verify_publish(conn, content_item_id=item, attempt="early",
                         fetcher_factory=fake_factory(), gsc_client_factory=gsc_factory)

    assert res["inspection"]["verdict"] == "PASS"
    assert seen[0]["meta"]["property"] == "sc-domain:example.com"
    insp = conn.execute(
        "select result from verify_events where content_item_id = %s and kind = 'inspection'",
        (item,),
    ).fetchone()
    assert insp["result"]["verdict"] == "PASS"
    assert insp["result"]["attempt"] == "early"


@requires_db
def test_verify_without_publish_event_raises(db_env, public_dns):
    conn, _org, _site, item = db_env
    with pytest.raises(LookupError):
        verify_publish(conn, content_item_id=item, attempt="early",
                       fetcher_factory=fake_factory())
