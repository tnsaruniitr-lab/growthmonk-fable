"""Rank tracker tests (Phase D0, agent A) — ZERO network; HTTP via httpx.MockTransport.

AIO extraction + rank detection helpers run DB-free; tracking/movement tests
require DATABASE_URL and skip cleanly without it.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import httpx
import pytest

from gm.intel import rank_tracker
from gm.intel import serp as serp_mod
from gm.intel.serp import DataForSeoClient, extract_ai_overview

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

LOGIN, PASSWORD = "login@example.com", "s3cret"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(serp_mod, "_sleep", lambda _s: None)


def make_dfs(responses: list[tuple[int, object]]) -> tuple[DataForSeoClient, list[httpx.Request]]:
    """MockTransport-backed client replaying `responses` in order (last repeats)."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status, body = responses[min(len(requests) - 1, len(responses) - 1)]
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return DataForSeoClient(login=LOGIN, password=PASSWORD, client=http_client), requests


# --- recorded-shape fixtures ---------------------------------------------------


def envelope(items: list[dict], *, cost: float = 0.003) -> dict:
    return {
        "version": "0.1.20240801",
        "status_code": 20000,
        "status_message": "Ok.",
        "cost": cost,
        "tasks_count": 1,
        "tasks_error": 0,
        "tasks": [
            {
                "id": "07040001-1535-0139-0000-aaaaaaaaaaaa",
                "status_code": 20000,
                "status_message": "Ok.",
                "cost": cost,
                "result_count": 1,
                "path": ["v3", "serp", "google", "organic", "live", "regular"],
                "result": [
                    {
                        "keyword": "botox dubai",
                        "type": "organic",
                        "se_domain": "google.ae",
                        "item_types": ["organic", "ai_overview"],
                        "items_count": len(items),
                        "items": items,
                    }
                ],
            }
        ],
    }


def organic(rank: int, url: str, domain: str | None = None) -> dict:
    entry = {
        "type": "organic",
        "rank_group": rank,
        "rank_absolute": rank,
        "url": url,
        "title": f"result {rank}",
        "description": "",
    }
    if domain is not None:
        entry["domain"] = domain
    return entry


# ai_overview variant 1: flat `references` array with url + domain fields
AIO_REFERENCES = {
    "type": "ai_overview",
    "rank_group": 1,
    "rank_absolute": 1,
    "references": [
        {
            "type": "ai_overview_reference",
            "source": "Glow Clinic",
            "domain": "www.glowclinic.ae",
            "url": "https://www.glowclinic.ae/botox",
        },
        {
            "type": "ai_overview_reference",
            "domain": "healthline.com",
            "url": "https://www.healthline.com/health/botox",
        },
    ],
}

# ai_overview variant 2: nested `items` elements carrying `links`, plus a
# top-level `links` array and a null references field — all must be tolerated.
AIO_NESTED = {
    "type": "ai_overview",
    "rank_group": 1,
    "items": [
        {
            "type": "ai_overview_element",
            "text": "Botox in Dubai typically costs AED 500-1500.",
            "links": [{"type": "link_element", "url": "https://blog.medspa-dubai.ae/botox-cost"}],
        },
        {"type": "ai_overview_element", "references": None},
    ],
    "links": [{"type": "link_element", "url": "https://www.dha.gov.ae/en/botox"}],
}

SERP_ITEMS = [
    organic(2, "https://www.medspa-dubai.ae/botox", "www.medspa-dubai.ae"),
    organic(1, "https://glowclinic.ae/botox", "glowclinic.ae"),
    organic(3, "https://listicle.com/best-botox"),  # no domain field -> derived from url
    AIO_REFERENCES,
]

SERP_OK = envelope(SERP_ITEMS)

TASK_ERROR_40501 = {
    "version": "0.1.20240801",
    "status_code": 20000,
    "status_message": "Ok.",
    "cost": 0,
    "tasks_count": 1,
    "tasks_error": 1,
    "tasks": [
        {
            "id": "07040002-1535-0139-0000-bbbbbbbbbbbb",
            "status_code": 40501,
            "status_message": "Invalid Field: 'location_name'.",
            "cost": 0,
            "result_count": 0,
            "result": None,
        }
    ],
}


# --- extract_ai_overview --------------------------------------------------------


def test_extract_aio_references_variant():
    out = extract_ai_overview(envelope([organic(1, "https://x.com/a"), AIO_REFERENCES]))
    assert out["present"] is True
    # www-stripped, deduped across url+domain fields, encounter order
    assert out["cited_domains"] == ["glowclinic.ae", "healthline.com"]


def test_extract_aio_nested_items_and_links_variant():
    out = extract_ai_overview(envelope([AIO_NESTED]))
    assert out["present"] is True
    assert out["cited_domains"] == ["blog.medspa-dubai.ae", "dha.gov.ae"]


def test_extract_aio_absent():
    out = extract_ai_overview(envelope([organic(1, "https://x.com/a")]))
    assert out == {"present": False, "cited_domains": []}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        [],
        "nope",
        {"tasks": None},
        {"tasks": []},
        {"tasks": [{"result": None}]},
        {"tasks": [{"result": [{"items": "not-a-list"}]}]},
        {"tasks": [{"result": [{"items": [{"type": "organic"}]}]}]},
    ],
)
def test_extract_aio_malformed_is_absent(raw):
    assert extract_ai_overview(raw) == {"present": False, "cited_domains": []}


def test_extract_aio_present_but_no_urls():
    bare = {"type": "ai_overview", "text": "some answer without any citations"}
    out = extract_ai_overview(envelope([bare]))
    assert out == {"present": True, "cited_domains": []}


# --- serp_live retains the AIO parse in features (snapshot carriage) ------------


def test_serp_live_features_carry_aio_parse():
    client, requests = make_dfs([(200, SERP_OK)])
    result = client.serp_live("botox dubai")
    assert len(requests) == 1

    # existing normalization unchanged: ranked organic entries
    assert [e["rank"] for e in result.organic] == [1, 2, 3]
    assert [e["domain"] for e in result.organic] == [
        "glowclinic.ae",
        "www.medspa-dubai.ae",
        "listicle.com",
    ]

    # the ai_overview feature carries the parsed citation hosts
    aio = [f for f in result.features if f["type"] == "ai_overview"]
    assert len(aio) == 1
    assert aio[0]["cited_domains"] == ["glowclinic.ae", "healthline.com"]


# --- rank detection (pure) -------------------------------------------------------


RESULTS = [
    {"rank": 1, "url": "https://glowclinic.ae/botox", "domain": "glowclinic.ae"},
    {"rank": 2, "url": "https://www.medspa-dubai.ae/botox", "domain": "www.medspa-dubai.ae"},
    {"rank": 3, "url": "https://blog.medspa-dubai.ae/costs", "domain": "blog.medspa-dubai.ae"},
    {"rank": 4, "url": "https://listicle.com/best-botox"},  # host derived from url
]


def test_find_rank_exact_domain():
    assert rank_tracker.find_rank(RESULTS, "glowclinic.ae") == (1, "https://glowclinic.ae/botox")


def test_find_rank_subdomain_aware_best_rank_wins():
    # www. and blog. both belong to medspa-dubai.ae; best (lowest) rank wins
    rank, url = rank_tracker.find_rank(RESULTS, "medspa-dubai.ae")
    assert rank == 2
    assert url == "https://www.medspa-dubai.ae/botox"


def test_find_rank_host_derived_from_url():
    assert rank_tracker.find_rank(RESULTS, "listicle.com") == (
        4,
        "https://listicle.com/best-botox",
    )


def test_find_rank_absent_is_none_never_zero():
    rank, url = rank_tracker.find_rank(RESULTS, "nowhere.example")
    assert rank is None
    assert rank != 0
    assert url is None
    assert rank_tracker.find_rank([], "glowclinic.ae") == (None, None)


def test_find_rank_no_reverse_substring_match():
    # client "clinic.ae" must NOT match "glowclinic.ae" (not a subdomain)
    assert rank_tracker.find_rank(RESULTS, "clinic.ae") == (None, None)


def test_top_domains_order_and_cutoff():
    results = [{"rank": r, "url": f"https://d{r}.com/x"} for r in range(12, 0, -1)]
    assert rank_tracker.top_domains(results) == [f"d{r}.com" for r in range(1, 11)]
    assert rank_tracker.top_domains(RESULTS) == [
        "glowclinic.ae",
        "medspa-dubai.ae",
        "blog.medspa-dubai.ae",
        "listicle.com",
    ]


def test_aio_from_features():
    features = [
        {"type": "local_pack"},
        {"type": "ai_overview", "cited_domains": ["blog.glowclinic.ae", "healthline.com"]},
    ]
    out = rank_tracker.aio_from_features(features, "glowclinic.ae")
    assert out["present"] is True
    assert out["cited"] is True  # subdomain-aware citation match
    assert out["cited_domains"] == ["blog.glowclinic.ae", "healthline.com"]

    not_cited = rank_tracker.aio_from_features(features, "medspa-dubai.ae")
    assert not_cited["present"] is True
    assert not_cited["cited"] is False

    absent = rank_tracker.aio_from_features([{"type": "local_pack"}], "glowclinic.ae")
    assert absent == {"present": False, "cited": False, "cited_domains": []}
    assert rank_tracker.aio_from_features([], "glowclinic.ae")["present"] is False


# --- DB tests ---------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    from gm import db

    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    from gm import db

    with db.connect(autocommit=True) as c:
        c.execute("truncate rank_history, tracked_queries restart identity cascade")
        c.execute("truncate serp_snapshots cascade")
        c.execute("truncate cost_events restart identity")
        yield c


@pytest.fixture()
def site(conn):
    org_id = conn.execute(
        "insert into orgs (name) values (%s) returning id", (f"t-{uuid.uuid4()}",)
    ).fetchone()["id"]
    site_id = conn.execute(
        "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
        (org_id, "glowclinic.ae"),
    ).fetchone()["id"]
    return {"org_id": org_id, "site_id": site_id}


def _history(conn):
    return conn.execute(
        "select * from rank_history order by query_norm, checked_on"
    ).fetchall()


@requires_db
class TestAddTrackedQuery:
    def test_insert_normalizes_and_upsert_reactivates(self, conn, site):
        qid = rank_tracker.add_tracked_query(
            conn, site["org_id"], site["site_id"], "  Botox   DUBAI ", target_page="/botox"
        )
        row = conn.execute("select * from tracked_queries").fetchone()
        assert str(row["id"]) == qid
        assert row["query_norm"] == "botox dubai"
        assert row["target_page"] == "/botox"
        assert row["active"] is True

        conn.execute("update tracked_queries set active = false")
        # re-add: same row, re-activated, None target_page does not clobber
        again = rank_tracker.add_tracked_query(
            conn, site["org_id"], site["site_id"], "botox dubai"
        )
        assert again == qid
        row = conn.execute("select * from tracked_queries").fetchone()
        assert row["active"] is True
        assert row["target_page"] == "/botox"
        assert conn.execute("select count(*) as n from tracked_queries").fetchone()["n"] == 1


@requires_db
class TestTrackSite:
    def test_track_and_same_day_idempotence(self, conn, site):
        rank_tracker.add_tracked_query(conn, site["org_id"], site["site_id"], "Botox Dubai")
        rank_tracker.add_tracked_query(conn, site["org_id"], site["site_id"], "fillers dubai")
        client, requests = make_dfs([(200, SERP_OK)])

        out = rank_tracker.track_site(
            conn, org_id=site["org_id"], site_id=site["site_id"], serp_client=client
        )
        assert out["queries"] == 2
        assert out["tracked"] == 2
        assert out["fresh"] == 2
        assert out["cached"] == 0
        assert out["errors"] == 0
        assert out["cost_cents"] == pytest.approx(0.6)  # 2 snapshots x $0.003
        assert len(requests) == 2

        rows = _history(conn)
        assert len(rows) == 2
        row = rows[0]
        assert row["query_norm"] == "botox dubai"
        assert row["checked_on"] == dt.date.today()
        assert row["rank"] == 1
        assert row["ranked_url"] == "https://glowclinic.ae/botox"
        assert row["aio_present"] is True
        assert row["aio_cited"] is True
        assert row["aio_cited_domains"] == ["glowclinic.ae", "healthline.com"]
        assert row["top_domains"] == ["glowclinic.ae", "medspa-dubai.ae", "listicle.com"]
        assert row["snapshot_id"] is not None

        # same-day re-run: cache hit (no purchase) + upsert (no duplicate rows)
        again = rank_tracker.track_site(
            conn, org_id=site["org_id"], site_id=site["site_id"], serp_client=client
        )
        assert again["fresh"] == 0
        assert again["cached"] == 2
        assert again["cost_cents"] == 0.0
        assert len(requests) == 2
        assert len(_history(conn)) == 2

    def test_weekly_cadence_max_age_six_days(self, conn, site):
        rank_tracker.add_tracked_query(conn, site["org_id"], site["site_id"], "botox dubai")
        client, requests = make_dfs([(200, SERP_OK)])

        rank_tracker.track_site(
            conn, org_id=site["org_id"], site_id=site["site_id"], serp_client=client
        )
        assert len(requests) == 1

        # 5-day-old snapshot is still inside max_age_days=6: reused, not re-bought
        conn.execute("update serp_snapshots set fetched_at = now() - interval '5 days'")
        out = rank_tracker.track_site(
            conn, org_id=site["org_id"], site_id=site["site_id"], serp_client=client
        )
        assert out["cached"] == 1
        assert len(requests) == 1

        # 7-day-old snapshot is stale: the weekly tick buys exactly one new one
        conn.execute("update serp_snapshots set fetched_at = now() - interval '7 days'")
        out = rank_tracker.track_site(
            conn, org_id=site["org_id"], site_id=site["site_id"], serp_client=client
        )
        assert out["fresh"] == 1
        assert len(requests) == 2

    def test_absent_rank_is_null_not_zero(self, conn, site):
        other_site = conn.execute(
            "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
            (site["org_id"], "nowhere.example"),
        ).fetchone()["id"]
        rank_tracker.add_tracked_query(conn, site["org_id"], other_site, "botox dubai")
        client, _ = make_dfs([(200, SERP_OK)])

        rank_tracker.track_site(
            conn, org_id=site["org_id"], site_id=other_site, serp_client=client
        )
        row = conn.execute(
            "select rank, ranked_url, aio_cited from rank_history where site_id = %s",
            (other_site,),
        ).fetchone()
        assert row["rank"] is None  # honest absence: NULL, never 0
        assert row["ranked_url"] is None
        assert row["aio_cited"] is False
        assert (
            conn.execute(
                "select count(*) as n from rank_history where site_id = %s and rank = 0",
                (other_site,),
            ).fetchone()["n"]
            == 0
        )

    def test_serp_error_counted_and_remaining_queries_tracked(self, conn, site):
        rank_tracker.add_tracked_query(conn, site["org_id"], site["site_id"], "botox dubai")
        rank_tracker.add_tracked_query(conn, site["org_id"], site["site_id"], "fillers dubai")
        # first purchase fails with a non-retryable task error, second succeeds
        client, requests = make_dfs([(200, TASK_ERROR_40501), (200, SERP_OK)])

        out = rank_tracker.track_site(
            conn, org_id=site["org_id"], site_id=site["site_id"], serp_client=client
        )
        assert out["errors"] == 1
        assert out["tracked"] == 1
        assert len(requests) == 2
        rows = _history(conn)
        assert len(rows) == 1
        assert rows[0]["query_norm"] == "fillers dubai"


@requires_db
class TestRankMovement:
    def _insert(self, conn, site, query, day, *, rank, aio_present=False, aio_cited=False,
                top=()):
        conn.execute(
            "insert into rank_history (org_id, site_id, query_norm, checked_on, rank,"
            " ranked_url, aio_present, aio_cited, top_domains)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                site["org_id"],
                site["site_id"],
                query,
                day,
                rank,
                f"https://glowclinic.ae/{query.replace(' ', '-')}" if rank else None,
                aio_present,
                aio_cited,
                list(top),
            ),
        )

    def test_movement_assembly(self, conn, site):
        d1, d2, d3 = dt.date(2026, 6, 1), dt.date(2026, 6, 22), dt.date(2026, 7, 1)
        self._insert(conn, site, "botox dubai", d1, rank=8,
                     top=["a.com", "b.com", "c.com"])
        self._insert(conn, site, "botox dubai", d2, rank=3, aio_present=True, aio_cited=True,
                     top=["a.com", "c.com", "d.com"])
        # outside the window: must not become the "last" row
        self._insert(conn, site, "botox dubai", d3, rank=1, top=["a.com"])
        # single-check query, never ranked
        self._insert(conn, site, "fillers dubai", d2, rank=None, top=["a.com"])

        out = rank_tracker.rank_movement(
            conn, site["site_id"], since=dt.date(2026, 6, 1), until=dt.date(2026, 6, 30)
        )
        assert [m["query"] for m in out] == ["botox dubai", "fillers dubai"]

        botox = out[0]
        assert (botox["first_date"], botox["last_date"]) == (d1, d2)
        assert (botox["first_rank"], botox["last_rank"]) == (8, 3)
        assert botox["first_aio_cited"] is False
        assert botox["last_aio_cited"] is True  # entered the AI Overview in-window
        assert botox["aio_present"] is True
        assert botox["entered_top10"] == ["d.com"]
        assert botox["left_top10"] == ["b.com"]

        fillers = out[1]
        assert fillers["first_date"] == fillers["last_date"] == d2
        assert fillers["first_rank"] is None
        assert fillers["last_rank"] is None
        assert fillers["entered_top10"] == []
        assert fillers["left_top10"] == []

    def test_empty_window(self, conn, site):
        assert (
            rank_tracker.rank_movement(
                conn, site["site_id"], since=dt.date(2026, 1, 1), until=dt.date(2026, 1, 31)
            )
            == []
        )
