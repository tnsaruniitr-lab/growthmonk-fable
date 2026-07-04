"""AdsPort tests (Phase D3, WP-G) — fixtures ONLY, the blocked-on-client rule.

ZERO network: every HTTP interaction goes through httpx.MockTransport replaying
recorded fixture shapes; no live ad account is ever contacted (none exists).
Reader parsing, retry/backoff, the read-only source-grep guarantee, and the
receipt rendering goldens run everywhere; DB-backed tests (readers_for_site,
pull_ads_daily slice replacement, roas_lines states, migration 011 constraint)
skip cleanly when DATABASE_URL is unset.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from gm.connections import ads
from gm.delivery import receipts
from gm.intel import ads_ingest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

SINCE = dt.date(2026, 6, 1)
UNTIL = dt.date(2026, 6, 7)
TODAY = dt.date(2026, 6, 8)  # window_range(7, today=TODAY) == (SINCE, UNTIL)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ads, "_sleep", lambda _s: None)


@pytest.fixture()
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(ads, "_sleep", calls.append)
    return calls


@pytest.fixture()
def google_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "dev-tok")
    monkeypatch.setenv("GOOGLE_ADS_REFRESH_TOKEN", "refresh-tok")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_ADS_CLIENT_SECRET", "client-secret")


def make_client(
    responses: list[tuple[int, object]],
) -> tuple[httpx.Client, list[httpx.Request]]:
    """MockTransport client replaying `responses` in order (last one repeats).

    The Google OAuth token endpoint is answered separately so reader tests can
    focus their response scripts on the report endpoint alone.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok-123"})
        requests.append(request)
        status, body = responses[min(len(requests) - 1, len(responses) - 1)]
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


# --- read-only by construction (the source-grep guarantee) -------------------------------


def test_ads_module_source_has_no_write_paths():
    """architecture §6: no mutate/write call exists anywhere in the module.

    Cheap and deterministic: grep the source for the Google Ads write verb and
    the Meta Marketing API write-path URL fragments; assert the only endpoints
    present are the report/insights reads.
    """
    src = Path(ads.__file__).read_text().lower()
    for fragment in (
        "mutate",              # every Google Ads write goes through *:mutate*
        "batchjob",            # bulk write surface
        "uploadclickconversion", "uploadconversion",  # conversion write-backs
        "/campaigns", "/adsets", "/adcreatives", "/adimages",  # Meta write paths
    ):
        assert fragment not in src, f"write-path fragment {fragment!r} found in ads.py"
    # The two read endpoints, and nothing else, are what this port speaks.
    assert "googleads:searchstream" in src
    assert "/insights" in src


# --- GoogleAdsReader (searchStream fixtures) ----------------------------------------------


SEARCH_STREAM = [
    {
        "results": [
            {
                "campaign": {"id": "111", "name": "Brand AE"},
                "metrics": {"costMicros": "12340000", "clicks": "17", "conversions": 2.5},
                "segments": {"date": "2026-06-01"},
                "customer": {"currencyCode": "AED"},
            },
            {   # provider had nothing for clicks/conversions -> None, never 0
                "campaign": {"id": "222", "name": "Generic"},
                "metrics": {"costMicros": "500000"},
                "segments": {"date": "2026-06-02"},
                "customer": {"currencyCode": "AED"},
            },
        ]
    },
    {   # second stream chunk — chunks concatenate
        "results": [
            {
                "campaign": {"id": "111", "name": "Brand AE"},
                "metrics": {"costMicros": "1000000", "clicks": "3", "conversions": 0.0},
                "segments": {"date": "2026-06-02"},
                "customer": {"currencyCode": "AED"},
            }
        ]
    },
]


def make_google(responses: list[tuple[int, object]]):
    client, requests = make_client(responses)
    reader = ads.GoogleAdsReader(
        customer_id="123-456-7890", login_customer_id="999-888-7777", client=client
    )
    return reader, requests


def test_google_reader_parses_stream_and_converts_micros(google_env):
    reader, requests = make_google([(200, SEARCH_STREAM)])
    rows = reader.daily_rows(since=SINCE, until=UNTIL)
    assert [r["campaign_id"] for r in rows] == ["111", "222", "111"]
    assert rows[0] == {
        "date": "2026-06-01", "campaign_id": "111", "campaign_name": "Brand AE",
        "spend": 12.34, "currency": "AED", "clicks": 17, "platform_conversions": 2.5,
    }
    # missing metrics are honest None, never fake zeros
    assert rows[1]["clicks"] is None
    assert rows[1]["platform_conversions"] is None
    assert rows[1]["spend"] == 0.5  # 500_000 micros
    # request shape: POST searchStream, dashes stripped, manager-link headers
    (req,) = requests
    assert req.method == "POST"
    assert req.url.path.endswith("/customers/1234567890/googleAds:searchStream")
    assert req.headers["developer-token"] == "dev-tok"
    assert req.headers["login-customer-id"] == "9998887777"
    assert req.headers["authorization"] == "Bearer tok-123"
    gaql = json.loads(req.content)["query"]
    assert "BETWEEN '2026-06-01' AND '2026-06-07'" in gaql
    assert "metrics.cost_micros" in gaql


def test_google_reader_missing_env_is_nonretryable(monkeypatch):
    for name in ("GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_REFRESH_TOKEN",
                 "GOOGLE_ADS_CLIENT_ID", "GOOGLE_ADS_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    reader, _ = make_google([(200, SEARCH_STREAM)])
    with pytest.raises(ads.AdsError) as exc_info:
        reader.daily_rows(since=SINCE, until=UNTIL)
    assert exc_info.value.retryable is False


def test_google_reader_retries_5xx_then_succeeds(google_env, sleeps):
    reader, requests = make_google([(500, "boom"), (429, "slow"), (200, SEARCH_STREAM)])
    rows = reader.daily_rows(since=SINCE, until=UNTIL)
    assert len(rows) == 3
    assert len(requests) == 3
    assert len(sleeps) == 2 and all(s >= 0 for s in sleeps)


def test_google_reader_exhausts_retries_as_retryable(google_env, sleeps):
    reader, requests = make_google([(503, "down")])
    with pytest.raises(ads.AdsError) as exc_info:
        reader.daily_rows(since=SINCE, until=UNTIL)
    assert exc_info.value.retryable is True
    assert len(requests) == ads.MAX_RETRIES + 1


def test_google_reader_401_is_auth_error_no_retry(google_env, sleeps):
    reader, requests = make_google([(401, "expired")])
    with pytest.raises(ads.AdsError) as exc_info:
        reader.daily_rows(since=SINCE, until=UNTIL)
    assert exc_info.value.retryable is False
    assert exc_info.value.auth_error is True
    assert len(requests) == 1 and sleeps == []


# --- MetaInsightsReader (insights fixtures, paginated) ------------------------------------


INSIGHTS_PAGE_1 = {
    "data": [
        {
            "date_start": "2026-06-01", "date_stop": "2026-06-01",
            "campaign_id": "801", "campaign_name": "Consult LP",
            "spend": "44.10", "clicks": "9", "account_currency": "AED",
            "actions": [
                {"action_type": "lead", "value": "2"},
                {"action_type": "link_click", "value": "9"},  # not a conversion
            ],
        },
    ],
    "paging": {"next": "https://graph.facebook.com/v20.0/act_42/insights?after=abc"},
}
INSIGHTS_PAGE_2 = {
    "data": [
        {   # no actions key and no clicks -> honest None
            "date_start": "2026-06-02", "date_stop": "2026-06-02",
            "campaign_id": "801", "campaign_name": "Consult LP",
            "spend": "12.00", "account_currency": "AED",
        },
    ],
}


def make_meta(responses: list[tuple[int, object]], token: str | None = "meta-tok"):
    client, requests = make_client(responses)
    reader = ads.MetaInsightsReader(act_id="act_42", token=token, client=client)
    return reader, requests


def test_meta_reader_paginates_and_parses(monkeypatch):
    reader, requests = make_meta([(200, INSIGHTS_PAGE_1), (200, INSIGHTS_PAGE_2)])
    rows = reader.daily_rows(since=SINCE, until=UNTIL)
    assert len(rows) == 2
    assert rows[0] == {
        "date": "2026-06-01", "campaign_id": "801", "campaign_name": "Consult LP",
        "spend": 44.10, "currency": "AED", "clicks": 9,
        "platform_conversions": 2.0,  # lead only; link_click is not a conversion
    }
    assert rows[1]["clicks"] is None
    assert rows[1]["platform_conversions"] is None
    # first request: GET act_{id}/insights with the contract params
    first = requests[0]
    assert first.method == "GET"
    assert first.url.path.endswith("/act_42/insights")
    assert first.url.params["level"] == "campaign"
    assert first.url.params["time_increment"] == "1"
    assert json.loads(first.url.params["time_range"]) == {
        "since": "2026-06-01", "until": "2026-06-07",
    }
    # second request followed paging.next verbatim
    assert requests[1].url.params["after"] == "abc"


def test_meta_reader_env_token(monkeypatch):
    monkeypatch.setenv("META_ADS_TOKEN", "env-tok")
    client, requests = make_client([(200, {"data": []})])
    reader = ads.MetaInsightsReader(act_id="42", client=client)
    assert reader.daily_rows(since=SINCE, until=UNTIL) == []
    assert requests[0].url.params["access_token"] == "env-tok"


def test_meta_reader_missing_token_is_nonretryable(monkeypatch):
    monkeypatch.delenv("META_ADS_TOKEN", raising=False)
    reader, _ = make_meta([(200, {"data": []})], token=None)
    with pytest.raises(ads.AdsError) as exc_info:
        reader.daily_rows(since=SINCE, until=UNTIL)
    assert exc_info.value.retryable is False


def test_meta_reader_403_is_auth_error(sleeps):
    reader, _ = make_meta([(403, "forbidden")])
    with pytest.raises(ads.AdsError) as exc_info:
        reader.daily_rows(since=SINCE, until=UNTIL)
    assert exc_info.value.auth_error is True
    assert sleeps == []


# --- window math ---------------------------------------------------------------------------


def test_window_range_ends_yesterday():
    assert ads_ingest.window_range(7, today=TODAY) == (SINCE, UNTIL)
    assert ads_ingest.window_range(1, today=TODAY) == (UNTIL, UNTIL)


# --- receipt rendering goldens (pure) -------------------------------------------------------


SITE = {"domain_norm": "ex.com"}


def _pm(**over) -> dict:
    base = {
        "status": "ok",
        "channels": [
            {"channel": "google_ads", "spend": 100.0, "currency": "AED",
             "clicks": 40, "platform_conversions": 5.0},
            {"channel": "meta_ads", "spend": 20.0, "currency": "AED",
             "clicks": None, "platform_conversions": None},
        ],
        "booked_consults": 3,
        "blended_cost_per_consult": 40.0,
        "prior": None,
        "note": None,
    }
    base.update(over)
    return base


def _render(paid_media) -> str:
    return receipts.render_receipt_html(SITE, {"period": "2026-06", "paid_media": paid_media})


def test_render_awaiting_ad_account_exact_line_no_table():
    html = _render({"status": "awaiting_ad_account", "channels": [],
                    "booked_consults": 0, "blended_cost_per_consult": None,
                    "prior": None, "note": "awaiting ad account connection"})
    assert "Blended cost per booked consult: awaiting ad account connection" in html
    section = html.split("<h2>Paid media</h2>")[1].split("</section>")[0]
    assert "<table" not in section  # no table, no zeros
    assert "0.00" not in section


def test_render_paid_media_before_beta_citations():
    html = _render(_pm())
    assert html.index("<h2>Paid media</h2>") < html.index("AI citation rates")


def test_render_no_spend_recorded_says_so():
    html = _render({"status": "no_spend_recorded", "channels": [], "booked_consults": 2,
                    "blended_cost_per_consult": None, "prior": None, "note": None})
    assert "No paid-media spend recorded this period." in html
    section = html.split("<h2>Paid media</h2>")[1].split("</section>")[0]
    assert "<table" not in section


def test_render_ok_rows_and_blended_line():
    html = _render(_pm())
    assert "google_ads" in html and "meta_ads" in html
    assert "100.00 AED" in html and "20.00 AED" in html
    assert "Blended cost per booked consult: 40.00 AED (3 booked consults)" in html
    # provider-absent clicks/conversions render as em-dash, never 0
    section = html.split("<h2>Paid media</h2>")[1].split("</section>")[0]
    assert "&mdash;" in section


def test_render_divide_by_zero_guard_never_zero_never_infinity():
    html = _render(_pm(booked_consults=0, blended_cost_per_consult=None,
                       note="not computable — 0 booked consults this period"))
    assert ("Blended cost per booked consult: not computable — "
            "0 booked consults this period") in html
    assert "inf" not in html.split("<h2>Paid media</h2>")[1].split("</section>")[0]


def test_render_mixed_currency_note_and_per_currency_rows():
    pm = _pm(
        channels=[
            {"channel": "google_ads", "spend": 100.0, "currency": "AED",
             "clicks": 4, "platform_conversions": None},
            {"channel": "meta_ads", "spend": 55.0, "currency": "USD",
             "clicks": 2, "platform_conversions": 1.0},
        ],
        blended_cost_per_consult=None,
        note="not computable — mixed currencies (AED, USD)",
    )
    html = _render(pm)
    assert "100.00 AED" in html and "55.00 USD" in html
    assert "mixed currencies (AED, USD)" in html
    assert "155" not in html  # the two currencies are never summed together


def test_render_prior_trend_arrow_only_when_both_computed():
    prior = _pm(blended_cost_per_consult=50.0)
    html = _render(_pm(prior=prior))
    assert "from 50.00" in html
    assert "delta-up" in html.split("<h2>Paid media</h2>")[1].split("</section>")[0]
    # prior absent -> no arrow
    html2 = _render(_pm(prior=None))
    assert "from 50.00" not in html2
    # prior in a different currency -> no cross-currency comparison
    prior_usd = _pm(blended_cost_per_consult=50.0)
    prior_usd["channels"] = [dict(prior_usd["channels"][0], currency="USD")]
    html3 = _render(_pm(prior=prior_usd))
    assert "from 50.00" not in html3


def test_render_hostile_strings_escaped():
    hostile = "<script>alert(1)</script>"
    pm = _pm(note=None)
    pm["channels"][0]["channel"] = hostile
    pm["channels"][0]["currency"] = hostile
    html = _render(pm)
    assert "<script" not in html
    assert "&lt;script&gt;" in html


def test_render_paid_media_section_absent_is_honest():
    html = receipts.render_receipt_html(SITE, {"period": "2026-06"})
    assert "Paid-media tracking is not available yet." in html


# --- DB fixtures ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    from gm import db

    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    from gm import db

    with db.connect(autocommit=True) as c:
        c.execute(
            "truncate orgs, sites, pages, page_url_history, audits, audit_findings,"
            " content_items, drafts, publish_events, verify_events, content_deltas,"
            " site_deltas, levers, queue_items, tracked_prompts, citation_runs,"
            " citation_results, connections, cost_events, jobs, booked_leads,"
            " ads_daily restart identity cascade"
        )
        yield c


def _org(conn) -> str:
    return conn.execute("insert into orgs (name) values ('t') returning id").fetchone()["id"]


def _site(conn, org, domain="ex.com") -> str:
    return conn.execute(
        "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
        (org, domain),
    ).fetchone()["id"]


def _connection(conn, org, site, kind, meta) -> str:
    """Ads connection row: NULL credentials, ids in meta (tokens live in env only)."""
    from psycopg.types.json import Jsonb

    return conn.execute(
        "insert into connections (org_id, site_id, kind, meta) values (%s, %s, %s, %s)"
        " returning id",
        (org, site, kind, Jsonb(meta)),
    ).fetchone()["id"]


def _booked(conn, org, site, when: dt.datetime) -> None:
    conn.execute(
        "insert into booked_leads (org_id, site_id, source, occurred_at, external_id)"
        " values (%s, %s, 'manual', %s, %s)",
        (org, site, when, f"lead-{uuid.uuid4()}"),
    )


class FakeReader:
    """Fixture-shaped AdsReader double for the ingest tests (never any live call)."""

    def __init__(self, channel: str, rows=None, error: Exception | None = None,
                 connection_id=None):
        self.channel = channel
        self._rows = rows or []
        self._error = error
        self.calls: list[tuple[dt.date, dt.date]] = []
        if connection_id is not None:
            self.connection_id = connection_id

    def daily_rows(self, *, since: dt.date, until: dt.date) -> list[dict]:
        self.calls.append((since, until))
        if self._error is not None:
            raise self._error
        return self._rows


def _row(date: str, *, campaign="c1", spend=10.0, currency="AED", clicks=5,
         conversions=1.0) -> dict:
    return {"date": date, "campaign_id": campaign, "campaign_name": campaign.upper(),
            "spend": spend, "currency": currency, "clicks": clicks,
            "platform_conversions": conversions}


# --- readers_for_site ------------------------------------------------------------------------


@requires_db
def test_readers_for_site_builds_ok_connections_only(conn, google_env):
    org = _org(conn)
    site = _site(conn, org)
    gid = _connection(conn, org, site, "google_ads",
                      {"customer_id": "123-456-7890", "login_customer_id": "999"})
    mid = _connection(conn, org, site, "meta_ads", {"act_id": "act_42"})
    _connection(conn, org, site, "whatsapp", {})  # other kinds never become readers
    readers = ads.readers_for_site(conn, site)
    assert [r.channel for r in readers] == ["google_ads", "meta_ads"]
    google, meta = readers
    assert isinstance(google, ads.GoogleAdsReader)
    assert google.customer_id == "1234567890"
    assert google.connection_id == gid
    assert isinstance(meta, ads.MetaInsightsReader)
    assert meta.act_id == "42"
    assert meta.connection_id == mid
    # broken connections are skipped until re-connected
    conn.execute("update connections set status = 'broken' where id = %s", (gid,))
    assert [r.channel for r in ads.readers_for_site(conn, site)] == ["meta_ads"]


# --- pull_ads_daily --------------------------------------------------------------------------


@requires_db
def test_pull_no_connections_is_honest_zero_work(conn):
    org = _org(conn)
    site = _site(conn, org)
    result = ads_ingest.pull_ads_daily(conn, org_id=org, site_id=site, today=TODAY)
    assert result == {"note": "no ads connections"}
    assert conn.execute("select count(*) as n from ads_daily").fetchone()["n"] == 0
    assert conn.execute("select count(*) as n from cost_events").fetchone()["n"] == 0


@requires_db
def test_pull_inserts_rows_and_records_zero_cost_event(conn):
    org = _org(conn)
    site = _site(conn, org)
    reader = FakeReader("google_ads", rows=[_row("2026-06-01"), _row("2026-06-02")])
    result = ads_ingest.pull_ads_daily(
        conn, org_id=org, site_id=site, readers=[reader], today=TODAY
    )
    assert result["channels"] == [{"channel": "google_ads", "rows": 2}]
    assert result["broken"] == []
    assert reader.calls == [(SINCE, UNTIL)]
    rows = conn.execute(
        "select * from ads_daily where site_id = %s order by date", (site,)
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["channel"] == "google_ads"
    assert float(rows[0]["spend"]) == 10.0
    assert rows[0]["currency"] == "AED"
    cost = conn.execute("select * from cost_events").fetchall()
    assert len(cost) == 1
    assert cost[0]["provider"] == "google_ads"
    assert cost[0]["purpose"] == "ads_daily_pull"
    assert float(cost[0]["cost_cents"]) == 0.0


@requires_db
def test_pull_slice_replacement_is_idempotent_and_restates(conn):
    org = _org(conn)
    site = _site(conn, org)
    rows = [_row("2026-06-01"), _row("2026-06-02")]
    for _ in range(2):  # double pull -> one slice, never duplicated
        ads_ingest.pull_ads_daily(
            conn, org_id=org, site_id=site,
            readers=[FakeReader("google_ads", rows=rows)], today=TODAY,
        )
    assert conn.execute("select count(*) as n from ads_daily").fetchone()["n"] == 2
    # restatement: the platform now reports only one day (conversions revised)
    ads_ingest.pull_ads_daily(
        conn, org_id=org, site_id=site,
        readers=[FakeReader("google_ads", rows=[_row("2026-06-01", conversions=7.0)])],
        today=TODAY,
    )
    left = conn.execute("select * from ads_daily").fetchall()
    assert len(left) == 1  # the whole window slice was replaced, stale day gone
    assert float(left[0]["platform_conversions"]) == 7.0
    # channels are independent slices: a meta pull never touches google rows
    ads_ingest.pull_ads_daily(
        conn, org_id=org, site_id=site,
        readers=[FakeReader("meta_ads", rows=[_row("2026-06-01", campaign="m1")])],
        today=TODAY,
    )
    assert conn.execute("select count(*) as n from ads_daily").fetchone()["n"] == 2


@requires_db
def test_pull_ignores_rows_outside_window_and_bad_dates(conn):
    org = _org(conn)
    site = _site(conn, org)
    rows = [_row("2026-06-01"), _row("2026-05-01"), _row("not-a-date")]
    result = ads_ingest.pull_ads_daily(
        conn, org_id=org, site_id=site,
        readers=[FakeReader("google_ads", rows=rows)], today=TODAY,
    )
    assert result["channels"] == [{"channel": "google_ads", "rows": 1}]


@requires_db
def test_pull_auth_error_marks_connection_broken_and_reports(conn):
    org = _org(conn)
    site = _site(conn, org)
    gid = _connection(conn, org, site, "google_ads",
                      {"customer_id": "1", "login_customer_id": "2"})
    broken_reader = FakeReader(
        "google_ads",
        error=ads.AdsError("ads: HTTP 401 auth failure", retryable=False, status_code=401),
        connection_id=gid,
    )
    ok_reader = FakeReader("meta_ads", rows=[_row("2026-06-01", currency="USD")])
    result = ads_ingest.pull_ads_daily(
        conn, org_id=org, site_id=site, readers=[broken_reader, ok_reader], today=TODAY,
    )
    row = conn.execute("select status, last_error from connections where id = %s",
                       (gid,)).fetchone()
    assert row["status"] == "broken"
    assert "401" in row["last_error"]
    assert result["broken"] == [{"channel": "google_ads",
                                 "error": "ads: HTTP 401 auth failure"}]
    assert result["channels"] == [{"channel": "meta_ads", "rows": 1}]  # others still pull


@requires_db
def test_pull_retryable_error_propagates_for_job_retry(conn):
    org = _org(conn)
    site = _site(conn, org)
    reader = FakeReader("google_ads",
                        error=ads.AdsError("ads: HTTP 503", retryable=True, status_code=503))
    with pytest.raises(ads.AdsError):
        ads_ingest.pull_ads_daily(conn, org_id=org, site_id=site, readers=[reader],
                                  today=TODAY)


@requires_db
def test_handle_pull_ads_daily_resolves_org_and_site(conn, monkeypatch):
    org = _org(conn)
    site = _site(conn, org)
    seen: dict = {}

    def fake_pull(c, *, org_id, site_id, readers=None, days=7, today=None):
        seen.update(org_id=org_id, site_id=site_id, days=days)
        return {"note": "no ads connections"}

    monkeypatch.setattr(ads_ingest, "pull_ads_daily", fake_pull)
    job = SimpleNamespace(id=1, site_id=None, org_id=None, payload={"site_id": str(site)})
    ads_ingest.handle_pull_ads_daily(SimpleNamespace(conn=conn, job=job))
    assert str(seen["site_id"]) == str(site)
    assert seen["org_id"] == org  # resolved from sites when the job carries none
    assert seen["days"] == 7  # default window when the payload carries no days
    job = SimpleNamespace(id=2, site_id=str(site), org_id=org,
                          payload={"days": 30})  # `gm ads pull --days 30` enqueues this
    ads_ingest.handle_pull_ads_daily(SimpleNamespace(conn=conn, job=job))
    assert seen["days"] == 30  # payload days honored, not silently dropped
    with pytest.raises(RuntimeError, match="requires site_id"):
        ads_ingest.handle_pull_ads_daily(
            SimpleNamespace(conn=conn, job=SimpleNamespace(site_id=None, org_id=None,
                                                           payload={}))
        )


# --- roas_lines (all states) ------------------------------------------------------------------


JUNE = dt.datetime(2026, 6, 10, 12, 0, tzinfo=dt.UTC)
MAY = dt.datetime(2026, 5, 10, 12, 0, tzinfo=dt.UTC)


def _ads_row(conn, org, site, date, *, channel="google_ads", spend=30.0, currency="AED",
             clicks=None, conversions=None):
    conn.execute(
        "insert into ads_daily (org_id, site_id, date, channel, campaign_id, spend,"
        " currency, clicks, platform_conversions)"
        " values (%s, %s, %s, %s, 'c1', %s, %s, %s, %s)",
        (org, site, date, channel, spend, currency, clicks, conversions),
    )


@requires_db
def test_roas_lines_awaiting_ad_account(conn):
    org = _org(conn)
    site = _site(conn, org)
    got = receipts.roas_lines(conn, site, "2026-06")
    assert got["status"] == "awaiting_ad_account"
    assert got["channels"] == []
    assert got["blended_cost_per_consult"] is None
    assert got["prior"] is None
    assert got["note"] == "awaiting ad account connection"


@requires_db
def test_roas_lines_no_spend_recorded(conn):
    org = _org(conn)
    site = _site(conn, org)
    _connection(conn, org, site, "google_ads", {"customer_id": "1", "login_customer_id": "2"})
    got = receipts.roas_lines(conn, site, "2026-06")
    assert got["status"] == "no_spend_recorded"
    assert got["channels"] == []
    assert got["blended_cost_per_consult"] is None


@requires_db
def test_roas_lines_ok_blended_and_prior(conn):
    org = _org(conn)
    site = _site(conn, org)
    _connection(conn, org, site, "google_ads", {"customer_id": "1", "login_customer_id": "2"})
    _ads_row(conn, org, site, dt.date(2026, 6, 1), spend=100.0, clicks=10, conversions=2.0)
    _ads_row(conn, org, site, dt.date(2026, 6, 2), spend=20.0)  # NULL clicks/conversions
    _ads_row(conn, org, site, dt.date(2026, 5, 3), spend=90.0, clicks=1)
    for _ in range(3):
        _booked(conn, org, site, JUNE)
    _booked(conn, org, site, MAY)
    got = receipts.roas_lines(conn, site, "2026-06")
    assert got["status"] == "ok"
    assert got["channels"] == [{
        "channel": "google_ads", "spend": 120.0, "currency": "AED",
        "clicks": 10, "platform_conversions": 2.0,
    }]
    assert got["booked_consults"] == 3
    assert got["blended_cost_per_consult"] == 40.0
    assert got["note"] is None
    prior = got["prior"]
    assert prior is not None and prior["status"] == "ok"
    assert prior["booked_consults"] == 1
    assert prior["blended_cost_per_consult"] == 90.0
    # a period whose prior has no rows carries prior=None (honest, arrow-free)
    assert receipts.roas_lines(conn, site, "2026-05")["prior"] is None


@requires_db
def test_roas_lines_zero_booked_never_zero_never_infinity(conn):
    org = _org(conn)
    site = _site(conn, org)
    _connection(conn, org, site, "meta_ads", {"act_id": "42"})
    _ads_row(conn, org, site, dt.date(2026, 6, 1), channel="meta_ads", spend=75.0)
    got = receipts.roas_lines(conn, site, "2026-06")
    assert got["status"] == "ok"
    assert got["booked_consults"] == 0
    assert got["blended_cost_per_consult"] is None  # never 0, never infinity
    assert got["note"] == "not computable — 0 booked consults this period"


@requires_db
def test_roas_lines_mixed_currency_law(conn):
    org = _org(conn)
    site = _site(conn, org)
    _connection(conn, org, site, "google_ads", {"customer_id": "1", "login_customer_id": "2"})
    _connection(conn, org, site, "meta_ads", {"act_id": "42"})
    _ads_row(conn, org, site, dt.date(2026, 6, 1), spend=100.0, currency="AED")
    _ads_row(conn, org, site, dt.date(2026, 6, 1), channel="meta_ads", spend=50.0,
             currency="USD")
    _booked(conn, org, site, JUNE)
    got = receipts.roas_lines(conn, site, "2026-06")
    assert got["status"] == "ok"
    # per-currency lines, never one summed number
    assert [(c["channel"], c["currency"], c["spend"]) for c in got["channels"]] == [
        ("google_ads", "AED", 100.0), ("meta_ads", "USD", 50.0),
    ]
    assert got["blended_cost_per_consult"] is None
    assert got["note"] == "not computable — mixed currencies (AED, USD)"
    # one channel reporting two currencies also splits into per-currency lines
    _ads_row(conn, org, site, dt.date(2026, 6, 2), spend=10.0, currency="USD")
    again = receipts.roas_lines(conn, site, "2026-06")
    assert len(again["channels"]) == 3


@requires_db
def test_assemble_site_receipt_carries_paid_media(conn):
    org = _org(conn)
    site = _site(conn, org)
    receipts.assemble_site_receipt(conn, site_id=site, period="2026-06")
    payload = conn.execute(
        "select payload from site_deltas where site_id = %s", (site,)
    ).fetchone()["payload"]
    assert payload["paid_media"]["status"] == "awaiting_ad_account"
    html = receipts.render_receipt_html({"domain_norm": "ex.com"}, payload)
    assert "Blended cost per booked consult: awaiting ad account connection" in html


# --- migration 011: queue_items kind list -----------------------------------------------------


@requires_db
def test_queue_kind_check_carries_local_presence_and_competitor_candidate(conn):
    org = _org(conn)
    site = _site(conn, org)
    for kind in ("local_presence", "competitor_candidate", "keyword_gap"):
        conn.execute(
            "insert into queue_items (org_id, site_id, kind, target, target_hash)"
            " values (%s, %s, %s, '{}', %s)",
            (org, site, kind, f"h-{kind}"),
        )
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into queue_items (org_id, site_id, kind, target, target_hash)"
            " values (%s, %s, 'bogus', '{}', 'h-x')",
            (org, site),
        )
