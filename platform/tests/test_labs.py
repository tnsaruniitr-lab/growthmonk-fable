"""DataForSEO Labs port tests — ZERO network; HTTP goes through httpx.MockTransport.

LabsClient fixtures follow the documented /v3/dataforseo_labs/google/ranked_keywords/live
response shape (items carry keyword_data.keyword_info.search_volume/cpc plus
ranked_serp_element.serp_item.rank_absolute/url). keyword_gap tests use a fake
Labs client and require DATABASE_URL; they skip cleanly without it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid

import httpx
import pytest

from gm.intel import labs as labs_mod
from gm.intel.labs import LabsClient, SerpError, keyword_gap

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

LOGIN, PASSWORD = "login@example.com", "s3cret"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(labs_mod, "_sleep", lambda _s: None)


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


def make_labs(responses: list[tuple[int, object]]) -> tuple[LabsClient, list[httpx.Request]]:
    http_client, requests = make_client(responses)
    return LabsClient(login=LOGIN, password=PASSWORD, client=http_client), requests


def body_of(request: httpx.Request):
    return json.loads(request.content)


# --- recorded-shape fixtures ---------------------------------------------------------


def labs_item(
    keyword: str,
    *,
    volume: int | None = 590,
    cpc: float | None = 3.1,
    rank: int = 4,
    url: str = "https://medspa-dubai.ae/botox",
    item_type: str = "organic",
) -> dict:
    return {
        "se_type": "google",
        "keyword_data": {
            "se_type": "google",
            "keyword": keyword,
            "location_code": 2784,
            "language_code": "en",
            "keyword_info": {
                "se_type": "google",
                "last_updated_time": "2026-06-28 04:11:12 +00:00",
                "competition": 0.61,
                "competition_level": "MEDIUM",
                "cpc": cpc,
                "search_volume": volume,
                "categories": [10012, 10237],
                "monthly_searches": [{"year": 2026, "month": 5, "search_volume": volume}],
            },
        },
        "ranked_serp_element": {
            "se_type": "google",
            "check_url": "https://www.google.ae/search?q=" + keyword.replace(" ", "+"),
            "serp_item": {
                "se_type": "google",
                "type": item_type,
                "rank_group": rank,
                "rank_absolute": rank,
                "position": "left",
                "xpath": "/html[1]/body[1]/div[1]",
                "domain": "medspa-dubai.ae",
                "title": f"{keyword} — MedSpa Dubai",
                "url": url,
                "etv": 12.3,
                "estimated_paid_traffic_cost": 40.2,
            },
            "serp_item_types": ["organic", "people_also_ask"],
            "se_results_count": 3210000,
            "last_updated_time": "2026-06-28 04:11:12 +00:00",
        },
    }


def labs_envelope(items: list[dict], *, cost: float | None = 0.0105) -> dict:
    return {
        "version": "0.1.20240801",
        "status_code": 20000,
        "status_message": "Ok.",
        "time": "0.41 sec.",
        "cost": cost,
        "tasks_count": 1,
        "tasks_error": 0,
        "tasks": [
            {
                "id": "07040912-1535-0387-0000-a1b2c3d4e5f6",
                "status_code": 20000,
                "status_message": "Ok.",
                "time": "0.39 sec.",
                "cost": cost,
                "result_count": 1,
                "path": ["v3", "dataforseo_labs", "google", "ranked_keywords", "live"],
                "data": {
                    "api": "dataforseo_labs",
                    "function": "ranked_keywords",
                    "se_type": "google",
                    "target": "medspa-dubai.ae",
                },
                "result": [
                    {
                        "se_type": "google",
                        "target": "medspa-dubai.ae",
                        "location_code": 2784,
                        "language_code": "en",
                        "total_count": 1240,
                        "items_count": len(items),
                        "items": items,
                        "metrics": {"organic": {"pos_1": 12, "count": 1240}},
                    }
                ],
            }
        ],
    }


TASK_ERROR_40501 = {
    "version": "0.1.20240801",
    "status_code": 20000,
    "status_message": "Ok.",
    "time": "0 sec.",
    "cost": 0,
    "tasks_count": 1,
    "tasks_error": 1,
    "tasks": [
        {
            "id": "07040915-1535-0387-0000-ba86a05ffa5f",
            "status_code": 40501,
            "status_message": "Invalid Field: 'target'.",
            "time": "0 sec.",
            "cost": 0,
            "result_count": 0,
            "path": ["v3", "dataforseo_labs", "google", "ranked_keywords", "live"],
            "data": {"api": "dataforseo_labs"},
            "result": None,
        }
    ],
}


# --- construction ----------------------------------------------------------------------


def test_missing_credentials_raise(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DATAFORSEO_LOGIN", raising=False)
    monkeypatch.delenv("DATAFORSEO_PASSWORD", raising=False)
    with pytest.raises(SerpError) as err:
        LabsClient()
    assert err.value.retryable is False


# --- ranked_keywords: request shape + normalization -------------------------------------


def test_ranked_keywords_normalization_and_request_shape():
    envelope = labs_envelope(
        [
            labs_item("Botox  Dubai", volume=1300, cpc=3.32, rank=4),
            labs_item("laser hair removal dubai", volume=880, cpc=2.1, rank=1,
                      url="https://medspa-dubai.ae/laser"),
        ]
    )
    client, requests = make_labs([(200, envelope)])
    out = client.ranked_keywords("medspa-dubai.ae")

    assert len(requests) == 1
    assert requests[0].url.path == "/v3/dataforseo_labs/google/ranked_keywords/live"
    assert requests[0].headers["Authorization"].startswith("Basic ")
    assert body_of(requests[0]) == [
        {
            "target": "medspa-dubai.ae",
            "location_code": 2784,
            "language_code": "en",
            "limit": 200,
            "item_types": ["organic"],
            "filters": ["ranked_serp_element.serp_item.rank_absolute", "<=", 20],
        }
    ]

    assert out == [
        {
            "query_norm": "botox dubai",  # keyword normalized
            "position": 4,
            "volume": 1300,
            "cpc": pytest.approx(3.32),
            "url": "https://medspa-dubai.ae/botox",
        },
        {
            "query_norm": "laser hair removal dubai",
            "position": 1,
            "volume": 880,
            "cpc": pytest.approx(2.1),
            "url": "https://medspa-dubai.ae/laser",
        },
    ]
    # envelope cost is dollars -> cents
    assert client.last_cost_cents == pytest.approx(1.05)


def test_ranked_keywords_params_forwarded():
    client, requests = make_labs([(200, labs_envelope([]))])
    client.ranked_keywords(
        "comp.ae", location_code=2840, language="de", limit=50, position_max=10
    )
    body = body_of(requests[0])[0]
    assert body["location_code"] == 2840
    assert body["language_code"] == "de"
    assert body["limit"] == 50
    assert body["filters"] == ["ranked_serp_element.serp_item.rank_absolute", "<=", 10]


def test_ranked_keywords_tolerates_malformed_and_filters():
    items = [
        labs_item("good kw", rank=3),
        labs_item("too deep", rank=15),                      # beyond position_max
        labs_item("paid kw", item_type="paid"),              # non-organic serp element
        labs_item("null volume", volume=None, cpc=None),     # nulls tolerated, not dropped
        {"se_type": "google"},                               # no keyword_data at all
        {"keyword_data": {"keyword": "no serp element"}},    # no ranked_serp_element
        "not-a-dict",                                        # junk entry
        {  # rank_absolute missing -> dropped (no honest position to report)
            "keyword_data": {"keyword": "rankless", "keyword_info": {"search_volume": 10}},
            "ranked_serp_element": {"serp_item": {"type": "organic", "url": "https://x.y/z"}},
        },
    ]
    client, _ = make_labs([(200, labs_envelope(items))])
    out = client.ranked_keywords("medspa-dubai.ae", position_max=10)
    assert [r["query_norm"] for r in out] == ["good kw", "null volume"]
    assert out[1] == {
        "query_norm": "null volume", "position": 4, "volume": None, "cpc": None,
        "url": "https://medspa-dubai.ae/botox",
    }


def test_ranked_keywords_empty_result():
    envelope = labs_envelope([])
    envelope["tasks"][0]["result"] = None  # provider returns null result on empty targets
    client, _ = make_labs([(200, envelope)])
    assert client.ranked_keywords("empty.ae") == []


def test_cost_fallback_formula_when_envelope_cost_missing():
    envelope = labs_envelope([labs_item("a"), labs_item("b"), labs_item("c")], cost=None)
    client, _ = make_labs([(200, envelope)])
    client.ranked_keywords("medspa-dubai.ae")
    # $0.01/task + $0.0001/row over 3 returned rows -> 1.03 cents
    assert client.last_cost_cents == pytest.approx(1.03)


# --- envelope / retry ---------------------------------------------------------------------


def test_task_error_40xxx_is_non_retryable():
    client, requests = make_labs([(200, TASK_ERROR_40501)])
    with pytest.raises(SerpError) as err:
        client.ranked_keywords("medspa-dubai.ae")
    assert err.value.retryable is False
    assert "40501" in str(err.value)
    assert len(requests) == 1


def test_retry_then_success_on_5xx():
    client, requests = make_labs([(500, "upstream boom"), (200, labs_envelope([labs_item("k")]))])
    out = client.ranked_keywords("medspa-dubai.ae")
    assert len(requests) == 2
    assert len(out) == 1


def test_retry_exhaustion_raises_retryable():
    client, requests = make_labs([(503, "down")])
    with pytest.raises(SerpError) as err:
        client.ranked_keywords("medspa-dubai.ae")
    assert err.value.retryable is True
    assert len(requests) == labs_mod.MAX_RETRIES + 1


def test_http_4xx_is_non_retryable():
    client, requests = make_labs([(404, "not found")])
    with pytest.raises(SerpError) as err:
        client.ranked_keywords("medspa-dubai.ae")
    assert err.value.retryable is False
    assert len(requests) == 1


# --- keyword_gap (DB) ----------------------------------------------------------------------


class FakeLabs:
    """In-memory stand-in for LabsClient: domain -> normalized rows, per-domain cost."""

    def __init__(
        self, rows_by_domain: dict[str, list[dict]], costs: dict[str, float] | None = None
    ):
        self.rows_by_domain = rows_by_domain
        self.costs = costs or {}
        self.calls: list[dict] = []
        self.last_cost_cents = 0.0

    def ranked_keywords(self, domain, *, location_code=2784, language="en", limit=200,
                        position_max=20):
        self.calls.append({"domain": domain, "limit": limit, "position_max": position_max})
        self.last_cost_cents = self.costs.get(domain, 1.5)
        return [dict(r) for r in self.rows_by_domain.get(domain, [])]


def gap_row(query: str, *, position: int = 3, volume: int | None = 100,
            url: str = "https://comp.ae/x") -> dict:
    return {"query_norm": query, "position": position, "volume": volume, "cpc": 1.0, "url": url}


@pytest.fixture(scope="session")
def _migrated():
    from gm import db

    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    from gm import db

    with db.connect(autocommit=True) as c:
        c.execute("truncate queue_items, rank_history cascade")
        c.execute("truncate gsc_window_agg, gsc_daily")
        c.execute("truncate cost_events restart identity")
        yield c


def make_site(conn, competitors: list[str]):
    org_id = conn.execute(
        "insert into orgs (name) values (%s) returning id", (f"labs-{uuid.uuid4().hex[:8]}",)
    ).fetchone()["id"]
    site_id = conn.execute(
        "insert into sites (org_id, domain_norm, competitor_domains)"
        " values (%s, %s, %s) returning id",
        (org_id, f"client-{uuid.uuid4().hex[:8]}.ae", competitors),
    ).fetchone()["id"]
    return {"org_id": org_id, "site_id": site_id}


def _rank_row(conn, site, query: str, rank: int | None):
    conn.execute(
        "insert into rank_history (org_id, site_id, query_norm, checked_on, rank)"
        " values (%s, %s, %s, current_date, %s)",
        (site["org_id"], site["site_id"], query, rank),
    )


def _window_row(conn, site, query: str, impressions: int):
    conn.execute(
        "insert into gsc_window_agg (site_id, window_days, page, query, clicks, impressions)"
        " values (%s, 28, 'https://e.x/p', %s, 0, %s)",
        (site["site_id"], query, impressions),
    )


def _daily_row(conn, site, query: str, impressions: int, day: dt.date):
    month = day.replace(day=1)
    nxt = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    conn.execute(
        f"create table if not exists gsc_daily_y{month.year}m{month.month:02d} "
        f"partition of gsc_daily for values from ('{month}') to ('{nxt}')"
    )
    conn.execute(
        "insert into gsc_daily (site_id, date, page, query, clicks, impressions)"
        " values (%s, %s, 'https://e.x/p', %s, 0, %s)",
        (site["site_id"], day, query, impressions),
    )


def _items(conn, site_id):
    return conn.execute(
        "select * from queue_items where site_id = %s and kind = 'keyword_gap'"
        " order by target->>'query'",
        (site_id,),
    ).fetchall()


def _run(conn, site, fake, **kwargs):
    return keyword_gap(
        conn, org_id=site["org_id"], site_id=site["site_id"], labs_client=fake, **kwargs
    )


@requires_db
def test_empty_competitors_honest_note(conn):
    site = make_site(conn, [])
    fake = FakeLabs({})
    result = keyword_gap(conn, org_id=site["org_id"], site_id=site["site_id"], labs_client=fake)
    assert result["competitors"] == []
    assert result["queued"] == 0
    assert result["cost_cents"] == 0.0
    assert "no competitor_domains" in result["note"]
    assert fake.calls == []
    assert _items(conn, site["site_id"]) == []
    assert conn.execute("select count(*) as n from cost_events").fetchone()["n"] == 0


@requires_db
def test_gap_filtering_double_client_present_exclusion_and_volume_floor(conn):
    site = make_site(conn, ["comp-a.ae"])
    # client-present sources
    _rank_row(conn, site, "ranked kw", 7)               # any rank -> excluded
    _rank_row(conn, site, "tracked but unranked", None)  # NULL rank = not present -> stays a gap
    _window_row(conn, site, "gsc agg kw", 40)            # impressions > 0 -> excluded
    _window_row(conn, site, "gsc zero impr kw", 0)       # zero impressions -> stays a gap
    _daily_row(conn, site, "gsc daily kw", 12, dt.date.today() - dt.timedelta(days=5))
    _daily_row(conn, site, "gsc stale kw", 12, dt.date.today() - dt.timedelta(days=40))

    fake = FakeLabs({
        "comp-a.ae": [
            gap_row("clean gap kw", position=2, volume=400),
            gap_row("ranked kw"),
            gap_row("tracked but unranked", volume=50),
            gap_row("gsc agg kw"),
            gap_row("gsc zero impr kw", volume=30),
            gap_row("gsc daily kw"),
            gap_row("gsc stale kw", volume=25),          # outside 28d window -> stays a gap
            gap_row("at floor kw", volume=10),           # boundary: volume_floor inclusive
            gap_row("below floor kw", volume=9),
            gap_row("no volume kw", volume=None),        # unknown demand fails the floor
            gap_row("too deep kw", position=11),         # defensive position re-filter
        ]
    })
    result = _run(conn, site, fake)

    items = _items(conn, site["site_id"])
    queued = {r["target"]["query"] for r in items}
    assert queued == {
        "clean gap kw", "tracked but unranked", "gsc zero impr kw", "gsc stale kw", "at floor kw",
    }
    assert result["candidates"] == result["queued"] == 5
    assert result["note"] is None
    assert fake.calls == [{"domain": "comp-a.ae", "limit": 200, "position_max": 10}]

    from gm.intel import detectors

    by_query = {r["target"]["query"]: r for r in items}
    clean = by_query["clean gap kw"]
    assert clean["at_stake"] == {
        "volume": 400, "best_competitor": "comp-a.ae", "their_position": 2, "basis": "labs",
    }
    assert clean["target_hash"] == detectors.target_hash({"query": "clean gap kw"})
    assert clean["status"] == "open"


@requires_db
def test_dedupe_keeps_best_position_then_volume(conn):
    site = make_site(conn, ["comp-a.ae", "comp-b.ae"])
    fake = FakeLabs({
        "comp-a.ae": [
            gap_row("shared kw", position=5, volume=900),
            gap_row("tie kw", position=3, volume=100),
        ],
        "comp-b.ae": [
            gap_row("shared kw", position=2, volume=300),  # better position wins over volume
            gap_row("tie kw", position=3, volume=250),     # same position: higher volume wins
        ],
    })
    result = _run(conn, site, fake)
    assert result["queued"] == 2
    assert [c["domain"] for c in fake.calls] == ["comp-a.ae", "comp-b.ae"]

    by_query = {r["target"]["query"]: r["at_stake"] for r in _items(conn, site["site_id"])}
    assert by_query["shared kw"] == {
        "volume": 300, "best_competitor": "comp-b.ae", "their_position": 2, "basis": "labs",
    }
    assert by_query["tie kw"] == {
        "volume": 250, "best_competitor": "comp-b.ae", "their_position": 3, "basis": "labs",
    }


@requires_db
def test_queue_upsert_discipline(conn):
    site = make_site(conn, ["comp-a.ae"])
    sid = site["site_id"]

    _run(conn, site, FakeLabs({"comp-a.ae": [gap_row("kw", volume=100)]}))
    first = _items(conn, sid)[0]
    assert first["status"] == "open"

    # open rows refresh at_stake, keep identity + first_seen
    _run(conn, site, FakeLabs({"comp-a.ae": [gap_row("kw", volume=200)]}))
    rows = _items(conn, sid)
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"]
    assert rows[0]["at_stake"]["volume"] == 200
    assert rows[0]["first_seen"] == first["first_seen"]

    # dismissed with a future snooze: untouched
    conn.execute(
        "update queue_items set status='dismissed', snooze_until = now() + interval '1 hour'"
        " where site_id = %s", (sid,),
    )
    _run(conn, site, FakeLabs({"comp-a.ae": [gap_row("kw", volume=300)]}))
    row = _items(conn, sid)[0]
    assert row["status"] == "dismissed"
    assert row["at_stake"]["volume"] == 200

    # dismissed with an elapsed snooze: reopens with fresh at_stake
    conn.execute(
        "update queue_items set snooze_until = now() - interval '1 second' where site_id = %s",
        (sid,),
    )
    _run(conn, site, FakeLabs({"comp-a.ae": [gap_row("kw", volume=300)]}))
    row = _items(conn, sid)[0]
    assert row["status"] == "open"
    assert row["snooze_until"] is None
    assert row["at_stake"]["volume"] == 300

    # actioned rows are never touched
    conn.execute("update queue_items set status='actioned' where site_id = %s", (sid,))
    _run(conn, site, FakeLabs({"comp-a.ae": [gap_row("kw", volume=999)]}))
    row = _items(conn, sid)[0]
    assert row["status"] == "actioned"
    assert row["at_stake"]["volume"] == 300


@requires_db
def test_cost_event_per_competitor(conn):
    site = make_site(conn, ["comp-a.ae", "comp-b.ae"])
    fake = FakeLabs(
        {"comp-a.ae": [gap_row("kw a")], "comp-b.ae": []},
        costs={"comp-a.ae": 1.01, "comp-b.ae": 1.0},
    )
    result = _run(conn, site, fake)
    assert result["cost_cents"] == pytest.approx(2.01)

    events = conn.execute("select * from cost_events order by id").fetchall()
    assert len(events) == 2
    assert {e["provider"] for e in events} == {"dataforseo"}
    assert {e["purpose"] for e in events} == {"labs_ranked_keywords"}
    assert events[0]["units"] == {"target": "comp-a.ae", "rows": 1}
    assert float(events[0]["cost_cents"]) == pytest.approx(1.01)
    assert events[1]["units"] == {"target": "comp-b.ae", "rows": 0}


@requires_db
def test_unknown_site_raises(conn):
    with pytest.raises(SerpError):
        keyword_gap(conn, org_id=uuid.uuid4(), site_id=uuid.uuid4(), labs_client=FakeLabs({}))


# --- job handler ----------------------------------------------------------------------


def _job(site, site_id=..., org_id=...):
    from gm.infra import jobs

    now = dt.datetime.now(dt.UTC)
    return jobs.JobRow(
        id=1, type="keyword_gap",
        org_id=site["org_id"] if org_id is ... else org_id,
        site_id=site["site_id"] if site_id is ... else site_id,
        payload={}, status="running", priority=5, run_after=now, attempts=1,
        max_attempts=3, idempotency_key=None, concurrency_key=None, locked_by="w",
        locked_until=None, last_error=None, created_at=now, finished_at=None,
    )


@requires_db
def test_handle_keyword_gap(conn, monkeypatch: pytest.MonkeyPatch):
    from gm.infra import jobs

    site = make_site(conn, ["comp-a.ae"])
    fake = FakeLabs({"comp-a.ae": [gap_row("handler kw", volume=80)]})
    monkeypatch.setattr(labs_mod, "LabsClient", lambda: fake)

    labs_mod.handle_keyword_gap(jobs.JobContext(_job(site), conn, "w", 60))
    items = _items(conn, site["site_id"])
    assert len(items) == 1
    assert items[0]["target"] == {"query": "handler kw"}


@requires_db
def test_handle_keyword_gap_resolves_org_from_site(conn):
    from gm.infra import jobs

    site = make_site(conn, [])  # empty competitors: no client constructed, no network
    labs_mod.handle_keyword_gap(jobs.JobContext(_job(site, org_id=None), conn, "w", 60))
    assert _items(conn, site["site_id"]) == []


@requires_db
def test_handle_keyword_gap_requires_site(conn):
    from gm.infra import jobs

    site = make_site(conn, [])
    with pytest.raises(RuntimeError, match="site_id"):
        labs_mod.handle_keyword_gap(jobs.JobContext(_job(site, site_id=None), conn, "w", 60))
