"""SerpDataPort tests — ZERO network; all HTTP goes through httpx.MockTransport.

Fixtures follow the documented DataForSEO response shapes (200 envelope with
tasks[0].status_code). Cache tests require DATABASE_URL and skip cleanly
without it; everything else runs DB-free.
"""

from __future__ import annotations

import json
import os
import uuid

import httpx
import pytest

from gm.intel import serp as serp_mod
from gm.intel.serp import DataForSeoClient, SerpError, get_snapshot, get_volumes, query_norm

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

LOGIN, PASSWORD = "login@example.com", "s3cret"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(serp_mod, "_sleep", lambda _s: None)


def make_client(responses: list[tuple[int, object]]) -> tuple[httpx.Client, list[httpx.Request]]:
    """MockTransport client replaying `responses` in order (last one repeats).

    Returns (client, requests) collecting the raw httpx.Request objects so tests
    can assert on auth headers and JSON bodies.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status, body = responses[min(len(requests) - 1, len(responses) - 1)]
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


def make_dfs(responses: list[tuple[int, object]]) -> tuple[DataForSeoClient, list[httpx.Request]]:
    http_client, requests = make_client(responses)
    return DataForSeoClient(login=LOGIN, password=PASSWORD, client=http_client), requests


def body_of(request: httpx.Request):
    return json.loads(request.content)


# --- recorded-shape fixtures -------------------------------------------------


def serp_envelope(items: list[dict], *, cost: float = 0.002, item_types=None) -> dict:
    return {
        "version": "0.1.20240801",
        "status_code": 20000,
        "status_message": "Ok.",
        "time": "0.21 sec.",
        "cost": cost,
        "tasks_count": 1,
        "tasks_error": 0,
        "tasks": [
            {
                "id": "07030942-1535-0139-0000-fd1a24c0f6a1",
                "status_code": 20000,
                "status_message": "Ok.",
                "time": "0.19 sec.",
                "cost": cost,
                "result_count": 1,
                "path": ["v3", "serp", "google", "organic", "live", "regular"],
                "data": {"api": "serp", "function": "live", "se": "google"},
                "result": [
                    {
                        "keyword": "botox dubai",
                        "type": "organic",
                        "se_domain": "google.ae",
                        "location_code": 2784,
                        "language_code": "en",
                        "check_url": "https://www.google.ae/search?q=botox+dubai",
                        "datetime": "2026-07-03 09:42:11 +00:00",
                        "spell": None,
                        "item_types": item_types
                        or ["organic", "people_also_ask", "local_pack"],
                        "se_results_count": 3210000,
                        "items_count": len(items),
                        "items": items,
                    }
                ],
            }
        ],
    }


SERP_ITEMS = [
    {
        "type": "local_pack",
        "rank_group": 1,
        "rank_absolute": 1,
        "title": "Glow Clinic",
        "rating": {"value": 4.9},
    },
    {  # deliberately out of order: rank_group 2 arrives before rank_group 1
        "type": "organic",
        "rank_group": 2,
        "rank_absolute": 3,
        "domain": "www.medspa-dubai.ae",
        "title": "Botox in Dubai — MedSpa",
        "description": "Prices and packages for botox in Dubai.",
        "url": "https://www.medspa-dubai.ae/botox",
        "breadcrumb": "medspa-dubai.ae > botox",
    },
    {
        "type": "organic",
        "rank_group": 1,
        "rank_absolute": 2,
        "domain": "glowclinic.ae",
        "title": "Botox Dubai | Glow Clinic",
        "description": "Botox from AED 599 at Glow Clinic.",
        "url": "https://glowclinic.ae/botox",
    },
    {
        "type": "people_also_ask",
        "rank_group": 3,
        "rank_absolute": 4,
        "items": [
            {
                "type": "people_also_ask_element",
                "title": "How much does botox cost in Dubai?",
                "seed_question": None,
                "xpath": "/html[1]/body[1]",
            },
            {"type": "people_also_ask_element", "title": "Is botox legal in the UAE?"},
            {"type": "people_also_ask_element"},  # missing title — must be tolerated
        ],
    },
    {
        "type": "organic",
        "rank_group": 3,
        "rank_absolute": 5,
        "title": "Best botox clinics",
        "url": "https://listicle.com/best-botox",  # no domain field — derived from url
    },
]

SERP_OK = serp_envelope(SERP_ITEMS)

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
            "id": "07030945-1535-0139-0000-ba86a05ffa5f",
            "status_code": 40501,
            "status_message": "Invalid Field: 'location_name'.",
            "time": "0 sec.",
            "cost": 0,
            "result_count": 0,
            "path": ["v3", "serp", "google", "organic", "live", "regular"],
            "data": {"api": "serp"},
            "result": None,
        }
    ],
}

API_ERROR_50000 = {"status_code": 50000, "status_message": "Internal Error.", "tasks": None}

VOLUME_OK = {
    "version": "0.1.20240801",
    "status_code": 20000,
    "status_message": "Ok.",
    "time": "1.2 sec.",
    "cost": 0.05,
    "tasks_count": 1,
    "tasks_error": 0,
    "tasks": [
        {
            "id": "07030951-1535-0139-0000-c6d33bd2a9d3",
            "status_code": 20000,
            "status_message": "Ok.",
            "time": "1.1 sec.",
            "cost": 0.05,
            "result_count": 2,
            "path": ["v3", "keywords_data", "google_ads", "search_volume", "live"],
            "data": {"api": "keywords_data"},
            "result": [
                {
                    "keyword": "botox dubai",
                    "spell": None,
                    "location_code": 2784,
                    "language_code": "en",
                    "search_partners": False,
                    "competition": "HIGH",
                    "competition_index": 85,
                    "search_volume": 1300,
                    "low_top_of_page_bid": 1.62,
                    "high_top_of_page_bid": 6.05,
                    "cpc": 3.32,
                    "monthly_searches": [
                        {"year": 2026, "month": 5, "search_volume": 1300},
                        {"year": 2026, "month": 4, "search_volume": 1000},
                    ],
                },
                {  # low-volume term: everything null — must be tolerated
                    "keyword": "hydrafacial deira promo",
                    "spell": None,
                    "location_code": 2784,
                    "language_code": "en",
                    "search_partners": False,
                    "competition": None,
                    "competition_index": None,
                    "search_volume": None,
                    "low_top_of_page_bid": None,
                    "high_top_of_page_bid": None,
                    "cpc": None,
                    "monthly_searches": None,
                },
            ],
        }
    ],
}


# --- query_norm / construction ----------------------------------------------


def test_query_norm():
    assert query_norm("  Botox   DUBAI \n") == "botox dubai"
    assert query_norm("botox dubai") == "botox dubai"


def test_missing_credentials_raise(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DATAFORSEO_LOGIN", raising=False)
    monkeypatch.delenv("DATAFORSEO_PASSWORD", raising=False)
    with pytest.raises(SerpError) as err:
        DataForSeoClient()
    assert err.value.retryable is False


# --- serp_live: envelope + normalization ------------------------------------


def test_serp_live_normalization_and_request_shape():
    client, requests = make_dfs([(200, SERP_OK)])
    result = client.serp_live("Botox  Dubai", depth=10)

    # request: one-task array, Basic auth, documented field names
    assert len(requests) == 1
    assert requests[0].url.path == "/v3/serp/google/organic/live/regular"
    auth = requests[0].headers["Authorization"]
    assert auth.startswith("Basic ")
    body = body_of(requests[0])
    assert body == [
        {
            "keyword": "Botox  Dubai",
            "location_name": "United Arab Emirates",
            "language_code": "en",
            "depth": 10,
        }
    ]

    # organic entries ranked (input order was 2, 1, 3)
    assert [entry["rank"] for entry in result.organic] == [1, 2, 3]
    assert [entry["domain"] for entry in result.organic] == [
        "glowclinic.ae",
        "www.medspa-dubai.ae",
        "listicle.com",  # derived from url when the domain field is absent
    ]
    top = result.organic[0]
    assert top["url"] == "https://glowclinic.ae/botox"
    assert top["title"] == "Botox Dubai | Glow Clinic"
    assert top["description"] == "Botox from AED 599 at Glow Clinic."
    assert top["type"] == "organic"

    # features: non-organic item types, PAA carries its question strings
    assert [f["type"] for f in result.features] == ["local_pack", "people_also_ask"]
    assert result.paa_questions == [
        "How much does botox cost in Dubai?",
        "Is botox legal in the UAE?",
    ]

    # cost: response field is dollars -> cents
    assert result.cost_cents == pytest.approx(0.2)
    assert client.last_cost_cents == pytest.approx(0.2)
    assert result.raw["status_code"] == 20000


def test_task_error_40xxx_is_non_retryable():
    client, requests = make_dfs([(200, TASK_ERROR_40501)])
    with pytest.raises(SerpError) as err:
        client.serp_live("botox dubai")
    assert err.value.retryable is False
    assert "40501" in str(err.value)
    assert len(requests) == 1  # envelope errors are not retried by the client


def test_top_level_error_50xxx_is_retryable_flagged():
    client, _ = make_dfs([(200, API_ERROR_50000)])
    with pytest.raises(SerpError) as err:
        client.serp_live("botox dubai")
    assert err.value.retryable is True


# --- retry/backoff -----------------------------------------------------------


def test_retry_then_success_on_5xx():
    client, requests = make_dfs([(500, "upstream boom"), (200, SERP_OK)])
    result = client.serp_live("botox dubai")
    assert len(requests) == 2
    assert len(result.organic) == 3


def test_http_4xx_is_non_retryable():
    client, requests = make_dfs([(404, "not found")])
    with pytest.raises(SerpError) as err:
        client.serp_live("botox dubai")
    assert err.value.retryable is False
    assert len(requests) == 1


def test_retry_exhaustion_raises_retryable():
    client, requests = make_dfs([(503, "down")])
    with pytest.raises(SerpError) as err:
        client.serp_live("botox dubai")
    assert err.value.retryable is True
    assert len(requests) == serp_mod.MAX_RETRIES + 1


# --- search_volume: null tolerance -------------------------------------------


def test_search_volume_null_tolerance():
    client, requests = make_dfs([(200, VOLUME_OK)])
    out = client.search_volume(["Botox  Dubai", "hydrafacial deira promo"])

    body = body_of(requests[0])
    assert requests[0].url.path == "/v3/keywords_data/google_ads/search_volume/live"
    assert body == [
        {
            "keywords": ["botox dubai", "hydrafacial deira promo"],
            "location_code": 2784,
            "language_code": "en",
        }
    ]

    assert out["botox dubai"] == {
        "volume": 1300,
        "cpc": pytest.approx(3.32),
        "competition": pytest.approx(0.85),  # competition_index / 100
    }
    assert out["hydrafacial deira promo"] == {"volume": None, "cpc": None, "competition": None}
    assert client.last_cost_cents == pytest.approx(5.0)


def test_search_volume_empty_queries_makes_no_request():
    client, requests = make_dfs([(200, VOLUME_OK)])
    assert client.search_volume([]) == {}
    assert requests == []


# --- reuse-before-buy cache (DB) ----------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    from gm import db

    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    from gm import db

    with db.connect(autocommit=True) as c:
        c.execute("truncate serp_snapshots, keyword_metrics cascade")
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


def _cost_events(conn):
    return conn.execute("select * from cost_events order by id").fetchall()


@requires_db
class TestGetSnapshot:
    def test_buy_then_reuse_then_ttl_expiry(self, conn, site):
        client, requests = make_dfs([(200, SERP_OK)])

        first = get_snapshot(conn, site["site_id"], "Botox  Dubai", client=client)
        assert first["fresh"] is True
        assert len(requests) == 1
        assert [e["rank"] for e in first["results"]] == [1, 2, 3]
        assert first["features"][1]["questions"][0] == "How much does botox cost in Dubai?"

        # snapshot row persisted with cost, org scoping, and the normalized query
        row = conn.execute("select * from serp_snapshots").fetchone()
        assert str(row["id"]) == first["id"]
        assert row["org_id"] == site["org_id"]
        assert row["query_norm"] == "botox dubai"
        assert float(row["cost_cents"]) == pytest.approx(0.2)

        events = _cost_events(conn)
        assert len(events) == 1
        assert events[0]["provider"] == "dataforseo"
        assert events[0]["purpose"] == "serp_live"
        assert float(events[0]["cost_cents"]) == pytest.approx(0.2)

        # second call inside the TTL: cache hit, no purchase
        second = get_snapshot(conn, site["site_id"], "botox dubai", client=client)
        assert second["fresh"] is False
        assert second["id"] == first["id"]
        assert second["results"] == first["results"]
        assert len(requests) == 1
        assert len(_cost_events(conn)) == 1

        # age the row past the TTL: bought again
        conn.execute("update serp_snapshots set fetched_at = now() - interval '8 days'")
        third = get_snapshot(conn, site["site_id"], "botox dubai", client=client)
        assert third["fresh"] is True
        assert third["id"] != first["id"]
        assert len(requests) == 2
        assert len(_cost_events(conn)) == 2

    def test_location_is_part_of_the_cache_key(self, conn, site):
        client, requests = make_dfs([(200, SERP_OK)])
        get_snapshot(conn, site["site_id"], "botox dubai", client=client)
        get_snapshot(
            conn, site["site_id"], "botox dubai", client=client, location="United States"
        )
        assert len(requests) == 2

    def test_unknown_site_raises(self, conn):
        client, _ = make_dfs([(200, SERP_OK)])
        with pytest.raises(SerpError):
            get_snapshot(conn, uuid.uuid4(), "botox dubai", client=client)


@requires_db
class TestGetVolumes:
    def test_buy_then_reuse_and_partial_miss(self, conn, site):
        client, requests = make_dfs([(200, VOLUME_OK)])

        out = get_volumes(
            conn, site["site_id"], ["Botox Dubai", "hydrafacial deira promo"], client=client
        )
        assert len(requests) == 1
        assert out["botox dubai"]["volume"] == 1300
        assert out["botox dubai"]["competition"] == pytest.approx(0.85)
        # null-volume term is stored too, so it is not re-bought
        assert out["hydrafacial deira promo"] == {
            "volume": None,
            "cpc": None,
            "competition": None,
        }
        assert conn.execute("select count(*) as n from keyword_metrics").fetchone()["n"] == 2

        events = _cost_events(conn)
        assert len(events) == 1
        assert events[0]["purpose"] == "search_volume"
        assert float(events[0]["cost_cents"]) == pytest.approx(5.0)

        # full cache hit: no request, no new cost event
        again = get_volumes(
            conn, site["site_id"], ["botox dubai", "hydrafacial deira promo"], client=client
        )
        assert again["botox dubai"]["cpc"] == pytest.approx(3.32)
        assert len(requests) == 1
        assert len(_cost_events(conn)) == 1

        # partial miss: only the new term is requested
        extra_client, extra_requests = make_dfs([(200, VOLUME_OK)])
        mixed = get_volumes(
            conn, site["site_id"], ["botox dubai", "fillers dubai"], client=extra_client
        )
        assert body_of(extra_requests[0])[0]["keywords"] == ["fillers dubai"]
        assert mixed["botox dubai"]["volume"] == 1300
        # provider fixture has no entry for the new term -> stored + returned as None
        assert mixed["fillers dubai"] == {"volume": None, "cpc": None, "competition": None}
        assert conn.execute("select count(*) as n from keyword_metrics").fetchone()["n"] == 3

    def test_ttl_expiry_rebuys(self, conn, site):
        client, requests = make_dfs([(200, VOLUME_OK)])
        get_volumes(conn, site["site_id"], ["botox dubai"], client=client)
        conn.execute("update keyword_metrics set fetched_at = now() - interval '31 days'")
        get_volumes(conn, site["site_id"], ["botox dubai"], client=client)
        assert len(requests) == 2
        # upsert keeps a single row per (site, query)
        assert conn.execute("select count(*) as n from keyword_metrics").fetchone()["n"] == 1

    def test_empty_queries_no_calls(self, conn, site):
        client, requests = make_dfs([(200, VOLUME_OK)])
        assert get_volumes(conn, site["site_id"], [], client=client) == {}
        assert requests == []
