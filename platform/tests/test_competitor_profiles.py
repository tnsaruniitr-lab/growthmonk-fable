"""Phase D2 WP-A tests — competitor profiles. ZERO network; HTTP goes through
httpx.MockTransport (envelopes reuse test_labs.labs_envelope). refresh tests use a
fake Labs client whose call log proves reuse-before-buy and the single bulk call;
DB tests require DATABASE_URL and skip cleanly without it."""

from __future__ import annotations

import datetime as dt
import os
import uuid

import psycopg
import pytest
from test_labs import TASK_ERROR_40501, body_of, labs_envelope, make_labs

from gm.intel import competitors as competitors_mod
from gm.intel import labs as labs_mod
from gm.intel.competitors import (
    MAX_COMPETITORS,
    latest_profile,
    refresh_competitor_profiles,
)
from gm.intel.labs import SerpError

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(labs_mod, "_sleep", lambda _s: None)


# --- recorded-shape fixtures ---------------------------------------------------------


def overview_organic(**over) -> dict:
    organic = {
        "pos_1": 12, "pos_2_3": 30, "pos_4_10": 58, "pos_11_20": 90, "pos_21_30": 40,
        "count": 1240, "etv": 5321.7, "impressions_etv": 810.2,
        "is_new": 14, "is_up": 33, "is_down": 21, "is_lost": 8,
    }
    organic.update(over)
    return organic


def overview_item(**over) -> dict:
    return {
        "se_type": "google",
        "location_code": 2784,
        "language_code": "en",
        "metrics": {
            "organic": overview_organic(**over),
            "paid": {"pos_1": 0, "count": 3, "etv": 12.4},
        },
    }


def bulk_item(target: str, *, count: int | None = 900, etv: float | None = 1234.5) -> dict:
    return {
        "se_type": "google",
        "target": target,
        "location_code": 2784,
        "language_code": "en",
        "metrics": {
            "organic": {"count": count, "etv": etv},
            "paid": {"count": 0, "etv": 0.0},
        },
    }


def competitor_item(
    domain: str,
    *,
    intersections: int | None = 42,
    avg_position: float | None = 12.3,
    count: int | None = 8800,
    etv: float | None = 9100.2,
) -> dict:
    return {
        "se_type": "google",
        "domain": domain,
        "avg_position": avg_position,
        "sum_position": 1230,
        "intersections": intersections,
        "full_domain_metrics": {
            "organic": {"count": count, "etv": etv},
            "paid": {"count": 1, "etv": 3.2},
        },
    }


# --- domain_rank_overview -------------------------------------------------------------


def test_domain_rank_overview_normalization_and_request_shape():
    client, requests = make_labs([(200, labs_envelope([overview_item()]))])
    out = client.domain_rank_overview("comp-a.ae")

    assert len(requests) == 1
    assert requests[0].url.path == "/v3/dataforseo_labs/google/domain_rank_overview/live"
    assert requests[0].headers["Authorization"].startswith("Basic ")
    assert body_of(requests[0]) == [
        {"target": "comp-a.ae", "location_code": 2784, "language_code": "en"}
    ]
    assert out == {
        "total_keywords": 1240,
        "top10_keywords": 100,  # pos_1 + pos_2_3 + pos_4_10 = 12 + 30 + 58
        "pos_1": 12,
        "movers": {"new": 14, "up": 33, "down": 21, "lost": 8},
        "raw": {
            "organic": overview_organic(),
            "paid": {"pos_1": 0, "count": 3, "etv": 12.4},
        },
    }
    assert client.last_cost_cents == pytest.approx(1.05)  # envelope dollars -> cents


def test_domain_rank_overview_params_forwarded():
    client, requests = make_labs([(200, labs_envelope([overview_item()]))])
    client.domain_rank_overview("comp.ae", location_code=2840, language="de")
    body = body_of(requests[0])[0]
    assert body["location_code"] == 2840
    assert body["language_code"] == "de"


def test_domain_rank_overview_empty_items_returns_none():
    client, _ = make_labs([(200, labs_envelope([]))])
    assert client.domain_rank_overview("empty.ae") is None


def test_domain_rank_overview_null_result_returns_none():
    envelope = labs_envelope([], cost=None)
    envelope["tasks"][0]["result"] = None  # provider returns null result on empty targets
    client, _ = make_labs([(200, envelope)])
    assert client.domain_rank_overview("empty.ae") is None
    assert client.last_cost_cents == pytest.approx(1.0)  # task-only fallback, zero rows


def test_domain_rank_overview_missing_buckets_stay_none():
    item = {"metrics": {"organic": {"count": 500, "etv": 7.5}}}
    client, _ = make_labs([(200, labs_envelope([item]))])
    out = client.domain_rank_overview("thin.ae")
    assert out == {
        "total_keywords": 500,
        "top10_keywords": None,  # no pos buckets at all -> no invented zero
        "pos_1": None,
        "movers": {"new": None, "up": None, "down": None, "lost": None},
        "raw": {"organic": {"count": 500, "etv": 7.5}},
    }


# --- bulk_traffic_estimation ------------------------------------------------------------


def test_bulk_traffic_estimation_request_shape_and_mapping():
    items = [
        bulk_item("www.comp-a.ae", count=900, etv=1234.5),  # www. stripped
        bulk_item("comp-b.ae", count=None, etv=None),       # nulls tolerated, key kept
        {"se_type": "google"},                              # no target -> skipped
        "not-a-dict",                                       # junk entry
    ]
    client, requests = make_labs([(200, labs_envelope(items))])
    out = client.bulk_traffic_estimation(["comp-a.ae", "comp-b.ae", "comp-c.ae"])

    assert len(requests) == 1
    assert requests[0].url.path == "/v3/dataforseo_labs/google/bulk_traffic_estimation/live"
    assert body_of(requests[0]) == [
        {
            "targets": ["comp-a.ae", "comp-b.ae", "comp-c.ae"],
            "location_code": 2784,
            "language_code": "en",
        }
    ]
    # comp-c.ae absent: the provider had nothing and the mapping stays honest
    assert out == {
        "comp-a.ae": {"est_traffic": 1234.5, "total_keywords": 900},
        "comp-b.ae": {"est_traffic": None, "total_keywords": None},
    }
    assert client.last_cost_cents == pytest.approx(1.05)


def test_bulk_traffic_estimation_empty_items():
    client, _ = make_labs([(200, labs_envelope([]))])
    assert client.bulk_traffic_estimation(["ghost.ae"]) == {}


# --- competitors_domain ------------------------------------------------------------------


def test_competitors_domain_request_shape_and_normalization():
    items = [
        competitor_item("www.rival-a.ae"),
        competitor_item("rival-b.ae", intersections=None),  # no overlap evidence -> dropped
        {"se_type": "google"},                              # no domain -> dropped
        "not-a-dict",
        competitor_item("rival-c.ae", intersections=7, avg_position=None,
                        count=None, etv=None),
    ]
    client, requests = make_labs([(200, labs_envelope(items))])
    out = client.competitors_domain("client.ae", limit=5)

    assert len(requests) == 1
    assert requests[0].url.path == "/v3/dataforseo_labs/google/competitors_domain/live"
    assert body_of(requests[0]) == [
        {"target": "client.ae", "location_code": 2784, "language_code": "en", "limit": 5}
    ]
    assert out == [
        {
            "domain": "rival-a.ae",
            "intersections": 42,
            "avg_position": 12.3,
            "their_keywords": 8800,
            "their_etv": 9100.2,
        },
        {
            "domain": "rival-c.ae",
            "intersections": 7,
            "avg_position": None,
            "their_keywords": None,
            "their_etv": None,
        },
    ]


def test_competitors_domain_default_limit_and_empty():
    client, requests = make_labs([(200, labs_envelope([]))])
    assert client.competitors_domain("client.ae") == []
    assert body_of(requests[0])[0]["limit"] == 30


# --- envelope / retry across all three endpoints -----------------------------------------

ENDPOINT_CALLS = [
    pytest.param(lambda c: c.domain_rank_overview("comp.ae"), id="domain_rank_overview"),
    pytest.param(lambda c: c.bulk_traffic_estimation(["comp.ae"]), id="bulk_traffic_estimation"),
    pytest.param(lambda c: c.competitors_domain("comp.ae"), id="competitors_domain"),
]


@pytest.mark.parametrize("call", ENDPOINT_CALLS)
def test_task_error_40501_is_non_retryable(call):
    client, requests = make_labs([(200, TASK_ERROR_40501)])
    with pytest.raises(SerpError) as err:
        call(client)
    assert err.value.retryable is False
    assert "40501" in str(err.value)
    assert len(requests) == 1


@pytest.mark.parametrize("call", ENDPOINT_CALLS)
def test_429_then_success_retries(call):
    client, requests = make_labs([(429, "slow down"), (200, labs_envelope([]))])
    call(client)  # must not raise: one retry after the 429 (with _sleep patched)
    assert len(requests) == 2


def test_cost_fallback_formula_per_endpoint():
    # $0.01/task + $0.0001/returned row when the envelope carries no cost field
    client, _ = make_labs([(200, labs_envelope([overview_item()], cost=None))])
    client.domain_rank_overview("comp.ae")
    assert client.last_cost_cents == pytest.approx(1.01)

    client, _ = make_labs(
        [(200, labs_envelope([bulk_item("a.ae"), bulk_item("b.ae")], cost=None))]
    )
    client.bulk_traffic_estimation(["a.ae", "b.ae"])
    assert client.last_cost_cents == pytest.approx(1.02)

    items = [competitor_item("x.ae"), competitor_item("y.ae"), competitor_item("z.ae")]
    client, _ = make_labs([(200, labs_envelope(items, cost=None))])
    client.competitors_domain("client.ae")
    assert client.last_cost_cents == pytest.approx(1.03)


# --- refresh_competitor_profiles (DB) -----------------------------------------------------


class FakeLabs:
    """In-memory LabsClient: overview rows per domain plus one bulk mapping."""

    def __init__(
        self,
        overviews: dict | None = None,
        bulk: dict | None = None,
        *,
        overview_costs: dict | None = None,
        bulk_cost: float = 0.9,
    ):
        self.overviews = overviews or {}
        self.bulk = bulk or {}
        self.overview_costs = overview_costs or {}
        self.bulk_cost = bulk_cost
        self.overview_calls: list[str] = []
        self.bulk_calls: list[list[str]] = []
        self.last_cost_cents = 0.0

    def domain_rank_overview(self, domain, *, location_code=2784, language="en"):
        self.overview_calls.append(domain)
        self.last_cost_cents = self.overview_costs.get(domain, 1.2)
        overview = self.overviews.get(domain)
        return dict(overview) if overview else None

    def bulk_traffic_estimation(self, domains, *, location_code=2784, language="en"):
        self.bulk_calls.append(list(domains))
        self.last_cost_cents = self.bulk_cost
        return {d: dict(self.bulk[d]) for d in domains if d in self.bulk}


def ov(total=1200, top10=45, pos_1=5, etv=880.5, movers=None) -> dict:
    return {
        "total_keywords": total,
        "top10_keywords": top10,
        "pos_1": pos_1,
        "movers": movers or {"new": 12, "up": 30, "down": 9, "lost": 4},
        "raw": {"organic": {"count": total, "etv": etv}},
    }


@pytest.fixture(scope="session")
def _migrated():
    from gm import db

    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    from gm import db

    with db.connect(autocommit=True) as c:
        c.execute("truncate competitor_profiles restart identity")
        c.execute("truncate cost_events restart identity")
        c.execute("truncate queue_items cascade")
        yield c


def make_site(conn, competitors: list[str]):
    org_id = conn.execute(
        "insert into orgs (name) values (%s) returning id", (f"cp-{uuid.uuid4().hex[:8]}",)
    ).fetchone()["id"]
    site_id = conn.execute(
        "insert into sites (org_id, domain_norm, competitor_domains)"
        " values (%s, %s, %s) returning id",
        (org_id, f"client-{uuid.uuid4().hex[:8]}.ae", competitors),
    ).fetchone()["id"]
    return {"org_id": org_id, "site_id": site_id}


def _run(conn, site, fake, **kwargs):
    return refresh_competitor_profiles(
        conn, org_id=site["org_id"], site_id=site["site_id"], labs_client=fake, **kwargs
    )


def _profiles(conn, site_id):
    return conn.execute(
        "select * from competitor_profiles where site_id = %s order by domain, checked_on",
        (site_id,),
    ).fetchall()


@requires_db
def test_refresh_empty_competitors_honest_note(conn):
    site = make_site(conn, [])
    fake = FakeLabs()
    result = _run(conn, site, fake)
    assert result["competitors"] == []
    assert (result["refreshed"], result["cached"], result["empty"]) == (0, 0, 0)
    assert result["cost_cents"] == 0.0
    assert "no competitor_domains" in result["note"]
    assert fake.overview_calls == [] and fake.bulk_calls == []
    assert _profiles(conn, site["site_id"]) == []
    assert conn.execute("select count(*) as n from cost_events").fetchone()["n"] == 0


@requires_db
def test_refresh_oversize_config_refused(conn):
    domains = [f"c{i}.ae" for i in range(MAX_COMPETITORS + 1)]
    site = make_site(conn, domains)
    fake = FakeLabs()
    result = _run(conn, site, fake)
    assert result["competitors"] == domains
    assert (result["refreshed"], result["cached"], result["empty"]) == (0, 0, 0)
    assert result["cost_cents"] == 0.0
    assert str(MAX_COMPETITORS) in result["note"] and "refused" in result["note"]
    assert fake.overview_calls == [] and fake.bulk_calls == []  # never silently truncates
    assert _profiles(conn, site["site_id"]) == []
    assert conn.execute("select count(*) as n from cost_events").fetchone()["n"] == 0


@requires_db
def test_refresh_buys_misses_with_single_bulk_call(conn):
    site = make_site(conn, ["comp-a.ae", "comp-b.ae"])
    fake = FakeLabs(
        overviews={
            "comp-a.ae": ov(total=1200, top10=45, etv=880.5),
            "comp-b.ae": ov(total=300, top10=7, etv=55.0),
        },
        bulk={"comp-a.ae": {"est_traffic": 1234.5, "total_keywords": 1190}},
        overview_costs={"comp-a.ae": 1.2, "comp-b.ae": 1.3},
    )
    result = _run(conn, site, fake)

    assert (result["refreshed"], result["cached"], result["empty"]) == (2, 0, 0)
    assert result["note"] is None
    assert result["cost_cents"] == pytest.approx(1.2 + 1.3 + 0.9)
    assert fake.overview_calls == ["comp-a.ae", "comp-b.ae"]
    assert fake.bulk_calls == [["comp-a.ae", "comp-b.ae"]]  # ONE bulk call for the misses

    rows = {r["domain"]: r for r in _profiles(conn, site["site_id"])}
    a, b = rows["comp-a.ae"], rows["comp-b.ae"]
    assert a["checked_on"] == dt.date.today()
    assert a["total_keywords"] == 1200          # overview count wins over the bulk count
    assert a["top10_keywords"] == 45
    assert float(a["est_traffic"]) == pytest.approx(1234.5)  # bulk_traffic_estimation wins
    assert a["movers"] == {"new": 12, "up": 30, "down": 9, "lost": 4}
    assert a["provider"] == "dataforseo"
    assert float(a["cost_cents"]) == pytest.approx(1.2)
    assert float(b["est_traffic"]) == pytest.approx(55.0)  # absent from bulk -> raw etv

    events = conn.execute("select * from cost_events order by id").fetchall()
    assert [e["purpose"] for e in events] == [
        "labs_domain_rank_overview", "labs_domain_rank_overview", "labs_bulk_traffic",
    ]
    assert {e["provider"] for e in events} == {"dataforseo"}
    assert events[0]["units"] == {"target": "comp-a.ae"}
    assert events[2]["units"] == {"targets": ["comp-a.ae", "comp-b.ae"], "rows": 1}
    assert float(events[2]["cost_cents"]) == pytest.approx(0.9)


@requires_db
def test_refresh_reuse_before_buy(conn):
    site = make_site(conn, ["comp-a.ae", "comp-b.ae"])
    overviews = {"comp-a.ae": ov(), "comp-b.ae": ov(total=300)}
    _run(conn, site, FakeLabs(overviews=overviews))

    # every row fresh: a re-run is a full cache hit — zero calls, zero spend
    fake2 = FakeLabs(overviews=overviews)
    result = _run(conn, site, fake2)
    assert (result["refreshed"], result["cached"], result["empty"]) == (0, 2, 0)
    assert result["cost_cents"] == 0.0
    assert result["note"] is None
    assert fake2.overview_calls == [] and fake2.bulk_calls == []
    assert conn.execute("select count(*) as n from cost_events").fetchone()["n"] == 3

    # one row stale: only that domain is bought, and the bulk call covers only it
    conn.execute(
        "update competitor_profiles set fetched_at = now() - interval '26 days'"
        " where site_id = %s and domain = 'comp-a.ae'",
        (site["site_id"],),
    )
    fake3 = FakeLabs(overviews=overviews)
    result = _run(conn, site, fake3)
    assert (result["refreshed"], result["cached"], result["empty"]) == (1, 1, 0)
    assert fake3.overview_calls == ["comp-a.ae"]
    assert fake3.bulk_calls == [["comp-a.ae"]]


@requires_db
def test_refresh_same_day_upsert_is_idempotent(conn):
    site = make_site(conn, ["comp-a.ae"])
    _run(conn, site, FakeLabs(overviews={"comp-a.ae": ov(total=100, top10=3)}))
    # max_age_days=0 forces a re-buy on the same day: the upsert hits the
    # (site_id, domain, checked_on) row instead of inserting a second one
    _run(
        conn, site,
        FakeLabs(overviews={"comp-a.ae": ov(total=150, top10=9)}),
        max_age_days=0,
    )
    rows = _profiles(conn, site["site_id"])
    assert len(rows) == 1
    assert rows[0]["total_keywords"] == 150
    assert rows[0]["top10_keywords"] == 9


@requires_db
def test_refresh_provider_empty_stores_nulls_row(conn):
    site = make_site(conn, ["ghost.ae", "quiet.ae"])
    fake = FakeLabs(
        overviews={"ghost.ae": None, "quiet.ae": None},
        bulk={"quiet.ae": {"est_traffic": 42.0, "total_keywords": 17}},
    )
    result = _run(conn, site, fake)
    # ghost.ae: nothing anywhere -> empty; quiet.ae: bulk-only data -> refreshed
    assert (result["refreshed"], result["cached"], result["empty"]) == (1, 0, 1)

    rows = {r["domain"]: r for r in _profiles(conn, site["site_id"])}
    ghost = rows["ghost.ae"]
    assert ghost["total_keywords"] is None and ghost["top10_keywords"] is None
    assert ghost["est_traffic"] is None
    assert ghost["movers"] == {} and ghost["raw_metrics"] == {}
    quiet = rows["quiet.ae"]
    assert quiet["total_keywords"] == 17
    assert float(quiet["est_traffic"]) == pytest.approx(42.0)
    assert quiet["top10_keywords"] is None

    # a NULLs row is "we checked, provider had nothing" — distinct from never-fetched
    profile = latest_profile(conn, site["site_id"], "ghost.ae")
    assert profile is not None
    assert profile["total_keywords"] is None and profile["est_traffic"] is None


@requires_db
def test_refresh_unknown_site_raises(conn):
    with pytest.raises(SerpError):
        refresh_competitor_profiles(
            conn, org_id=uuid.uuid4(), site_id=uuid.uuid4(), labs_client=FakeLabs()
        )


# --- latest_profile -------------------------------------------------------------------


@requires_db
def test_latest_profile_none_when_never_fetched(conn):
    site = make_site(conn, ["comp-a.ae"])
    assert latest_profile(conn, site["site_id"], "comp-a.ae") is None


@requires_db
def test_latest_profile_returns_newest_row(conn):
    site = make_site(conn, ["comp-a.ae"])
    from psycopg.types.json import Jsonb

    for checked_on, total, etv in (
        (dt.date.today() - dt.timedelta(days=30), 100, 10.5),
        (dt.date.today(), 140, 12.5),
    ):
        conn.execute(
            "insert into competitor_profiles (org_id, site_id, domain, checked_on,"
            " total_keywords, top10_keywords, est_traffic, movers)"
            " values (%s, %s, 'comp-a.ae', %s, %s, 4, %s, %s)",
            (site["org_id"], site["site_id"], checked_on, total, etv, Jsonb({"new": 1})),
        )
    assert latest_profile(conn, site["site_id"], "comp-a.ae") == {
        "domain": "comp-a.ae",
        "total_keywords": 140,
        "top10_keywords": 4,
        "est_traffic": 12.5,
        "movers": {"new": 1},
        "checked_on": dt.date.today(),
    }


# --- job handler ----------------------------------------------------------------------


def _job(site, site_id=..., org_id=..., payload=None):
    from gm.infra import jobs

    now = dt.datetime.now(dt.UTC)
    return jobs.JobRow(
        id=1, type="refresh_competitor_profiles",
        org_id=site["org_id"] if org_id is ... else org_id,
        site_id=site["site_id"] if site_id is ... else site_id,
        payload=payload or {}, status="running", priority=5, run_after=now, attempts=1,
        max_attempts=3, idempotency_key=None, concurrency_key=None, locked_by="w",
        locked_until=None, last_error=None, created_at=now, finished_at=None,
    )


@requires_db
def test_handler_site_scoped_resolves_org_from_site(conn, monkeypatch: pytest.MonkeyPatch):
    from gm.infra import jobs

    site = make_site(conn, ["comp-a.ae"])
    fake = FakeLabs(overviews={"comp-a.ae": ov()})
    monkeypatch.setattr(competitors_mod, "LabsClient", lambda: fake)

    ctx = jobs.JobContext(_job(site, org_id=None), conn, "w", 60)
    competitors_mod.handle_refresh_competitor_profiles(ctx)
    rows = _profiles(conn, site["site_id"])
    assert len(rows) == 1
    assert rows[0]["org_id"] == site["org_id"]


@requires_db
def test_handler_accepts_payload_site_id(conn, monkeypatch: pytest.MonkeyPatch):
    from gm.infra import jobs

    site = make_site(conn, ["comp-a.ae"])
    fake = FakeLabs(overviews={"comp-a.ae": ov()})
    monkeypatch.setattr(competitors_mod, "LabsClient", lambda: fake)

    job = _job(site, site_id=None, org_id=None, payload={"site_id": str(site["site_id"])})
    competitors_mod.handle_refresh_competitor_profiles(jobs.JobContext(job, conn, "w", 60))
    assert len(_profiles(conn, site["site_id"])) == 1


@requires_db
def test_handler_unknown_site_raises(conn):
    from gm.infra import jobs

    site = make_site(conn, [])
    job = _job(site, site_id=uuid.uuid4(), org_id=None)
    with pytest.raises(RuntimeError, match="site not found"):
        competitors_mod.handle_refresh_competitor_profiles(jobs.JobContext(job, conn, "w", 60))


@requires_db
def test_handler_without_site_id_covers_configured_sites(
    conn, monkeypatch: pytest.MonkeyPatch
):
    from gm.infra import jobs

    with_comp = make_site(conn, ["comp-a.ae"])
    without = make_site(conn, [])
    fake = FakeLabs(overviews={"comp-a.ae": ov()})
    monkeypatch.setattr(competitors_mod, "LabsClient", lambda: fake)

    job = _job(with_comp, site_id=None, org_id=None)
    competitors_mod.handle_refresh_competitor_profiles(jobs.JobContext(job, conn, "w", 60))

    assert "comp-a.ae" in fake.overview_calls
    rows = _profiles(conn, with_comp["site_id"])
    assert [r["domain"] for r in rows] == ["comp-a.ae"]
    assert _profiles(conn, without["site_id"]) == []  # empty config never bought


# --- migration 010 schema -------------------------------------------------------------


@requires_db
def test_queue_items_accepts_competitor_candidate_kind(conn):
    site = make_site(conn, [])
    conn.execute(
        "insert into queue_items (org_id, site_id, kind, target, target_hash)"
        " values (%s, %s, 'competitor_candidate', '{\"domain\": \"rival.ae\"}', 'h1')",
        (site["org_id"], site["site_id"]),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into queue_items (org_id, site_id, kind, target, target_hash)"
            " values (%s, %s, 'bogus_kind', '{}', 'h2')",
            (site["org_id"], site["site_id"]),
        )


@requires_db
def test_tracked_queries_serp_depth_default_and_check(conn):
    site = make_site(conn, [])
    row = conn.execute(
        "insert into tracked_queries (org_id, site_id, query_norm)"
        " values (%s, %s, 'default depth kw') returning serp_depth",
        (site["org_id"], site["site_id"]),
    ).fetchone()
    assert row["serp_depth"] == 10
    row = conn.execute(
        "insert into tracked_queries (org_id, site_id, query_norm, serp_depth)"
        " values (%s, %s, 'deep kw', 100) returning serp_depth",
        (site["org_id"], site["site_id"]),
    ).fetchone()
    assert row["serp_depth"] == 100
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into tracked_queries (org_id, site_id, query_norm, serp_depth)"
            " values (%s, %s, 'bad depth kw', 50)",
            (site["org_id"], site["site_id"]),
        )


@requires_db
def test_serp_snapshots_depth_default(conn):
    site = make_site(conn, [])
    row = conn.execute(
        "insert into serp_snapshots (org_id, site_id, query_norm)"
        " values (%s, %s, 'depth kw') returning depth",
        (site["org_id"], site["site_id"]),
    ).fetchone()
    assert row["depth"] == 10
