"""Phase D2 WP-C tests — feature share, competitive position, receipt section.

ZERO network: HTTP goes through httpx.MockTransport (test_labs.py pattern).
Pure tests (owner retention, week math, renderer goldens) run everywhere;
DB-backed tests (depth cache rule, weekly buckets, medians, has_data law,
assembly) require DATABASE_URL and skip cleanly without it.

gm.intel.competitors (WP-A) is built concurrently: every test that touches
profiles fakes the feature_share._latest_profile_fn accessor, so this file
passes with or without WP-A's module present.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid

import httpx
import pytest
from psycopg.types.json import Jsonb

from gm.delivery import receipts
from gm.intel import feature_share as fs
from gm.intel import serp as serp_mod
from gm.intel.serp import DataForSeoClient, get_snapshot

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

LOGIN, PASSWORD = "login@example.com", "s3cret"

SINCE, UNTIL = dt.date(2026, 6, 1), dt.date(2026, 6, 30)  # 2026-06-01 is a Monday


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


def body_of(request: httpx.Request):
    return json.loads(request.content)


# --- recorded-shape fixtures ---------------------------------------------------------------


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


FEATURED_SNIPPET = {
    "type": "featured_snippet",
    "rank_group": 1,
    "domain": "www.medspa-dubai.ae",
    "title": "Botox cost in Dubai",
    "url": "https://www.medspa-dubai.ae/botox-cost",
}

PAA = {
    "type": "people_also_ask",
    "items": [
        {
            "type": "people_also_ask_element",
            "title": "How much does botox cost in Dubai?",
            "expanded_element": [
                {
                    "type": "people_also_ask_expanded_element",
                    "url": "https://www.glowclinic.ae/botox-faq",
                    "domain": "www.glowclinic.ae",
                }
            ],
        },
        {
            "type": "people_also_ask_element",
            "title": "Is botox safe?",
            "expanded_element": [
                {
                    "type": "people_also_ask_expanded_element",
                    "url": "https://www.healthline.com/health/botox",
                    "domain": "www.healthline.com",
                },
                # dedupes against the first question's source
                {"type": "people_also_ask_expanded_element", "url": "https://glowclinic.ae/x"},
            ],
        },
    ],
}


# --- owner retention in _normalize_items (pure, via serp_live) ------------------------------


def _features_of(items: list[dict]) -> dict[str, dict]:
    client, _requests = make_dfs([(200, envelope(items))])
    result = client.serp_live("botox dubai")
    return {f["type"]: f for f in result.features}


def test_featured_snippet_owner_retained_and_normalized():
    feats = _features_of([organic(1, "https://x.com/a"), FEATURED_SNIPPET])
    snippet = feats["featured_snippet"]
    assert snippet["domain"] == "medspa-dubai.ae"  # www-stripped
    assert snippet["url"] == "https://www.medspa-dubai.ae/botox-cost"


def test_featured_snippet_url_only_derives_domain():
    item = {"type": "featured_snippet", "url": "https://blog.rival.ae/post"}
    snippet = _features_of([item])["featured_snippet"]
    assert snippet["domain"] == "blog.rival.ae"
    assert snippet["url"] == "https://blog.rival.ae/post"


def test_featured_snippet_without_owner_fields_stays_bare():
    snippet = _features_of([{"type": "featured_snippet", "title": "?"}])["featured_snippet"]
    assert "domain" not in snippet
    assert "url" not in snippet


def test_paa_source_domains_normalized_deduped_questions_kept():
    paa = _features_of([PAA])["people_also_ask"]
    assert paa["questions"] == ["How much does botox cost in Dubai?", "Is botox safe?"]
    assert paa["source_domains"] == ["glowclinic.ae", "healthline.com"]


def test_paa_without_sources_retains_empty_list():
    item = {
        "type": "people_also_ask",
        "items": [{"type": "people_also_ask_element", "title": "Q?"}],
    }
    paa = _features_of([item])["people_also_ask"]
    assert paa["questions"] == ["Q?"]
    assert paa["source_domains"] == []


# --- _feature_owners: legacy rows never guessed (pure) ---------------------------------------


def test_feature_owners_legacy_rows_are_none():
    assert fs._feature_owners("featured_snippet", {"type": "featured_snippet"}) is None
    assert fs._feature_owners("ai_overview", {"type": "ai_overview"}) is None
    legacy_paa = {"type": "people_also_ask", "questions": ["q"]}
    assert fs._feature_owners("people_also_ask", legacy_paa) is None


def test_feature_owners_retained_but_sourceless_is_empty():
    assert fs._feature_owners("ai_overview", {"type": "ai_overview", "cited_domains": []}) == []
    paa = {"type": "people_also_ask", "source_domains": []}
    assert fs._feature_owners("people_also_ask", paa) == []


def test_feature_owners_normalize_and_dedupe():
    snippet = {"type": "featured_snippet", "domain": "www.a.com", "url": "https://www.a.com/x"}
    assert fs._feature_owners("featured_snippet", snippet) == ["a.com"]
    aio = {"type": "ai_overview", "cited_domains": ["www.a.com", "a.com", "b.com", 7, ""]}
    assert fs._feature_owners("ai_overview", aio) == ["a.com", "b.com"]


def test_week_start_is_monday():
    assert fs.week_start(dt.date(2026, 6, 1)) == dt.date(2026, 6, 1)  # Monday
    assert fs.week_start(dt.date(2026, 6, 7)) == dt.date(2026, 6, 1)  # Sunday
    assert fs.week_start(dt.date(2026, 6, 10)) == dt.date(2026, 6, 8)


# --- DB fixtures ------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    from gm import db

    db.run_migrations()
    # WP-A's migration 010 lands concurrently; keep this file self-sufficient
    # by applying the exact COMMON DDL when the columns are missing (the
    # integrator re-verifies against the real 010).
    with db.connect(autocommit=True) as c:
        def missing(table: str, column: str) -> bool:
            return (
                c.execute(
                    "select 1 from information_schema.columns"
                    " where table_name = %s and column_name = %s",
                    (table, column),
                ).fetchone()
                is None
            )

        if missing("serp_snapshots", "depth"):
            c.execute("alter table serp_snapshots add column depth int not null default 10")
        if missing("tracked_queries", "serp_depth"):
            c.execute(
                "alter table tracked_queries add column serp_depth int not null default 10"
                " check (serp_depth in (10,100))"
            )


@pytest.fixture()
def conn(_migrated):
    from gm import db

    with db.connect(autocommit=True) as c:
        c.execute("truncate rank_history, tracked_queries restart identity cascade")
        c.execute("truncate serp_snapshots, audits, site_deltas cascade")
        c.execute("truncate cost_events restart identity")
        yield c


@pytest.fixture()
def site(conn):
    org_id = conn.execute(
        "insert into orgs (name) values (%s) returning id", (f"t-{uuid.uuid4()}",)
    ).fetchone()["id"]
    site_id = conn.execute(
        "insert into sites (org_id, domain_norm, competitor_domains)"
        " values (%s, %s, %s) returning id",
        (org_id, "glowclinic.ae", ["medspa-dubai.ae", "rival.ae"]),
    ).fetchone()["id"]
    return {"org_id": org_id, "site_id": site_id}


def _track(conn, site, query: str, active: bool = True) -> None:
    conn.execute(
        "insert into tracked_queries (org_id, site_id, query_norm, active)"
        " values (%s, %s, %s, %s)",
        (site["org_id"], site["site_id"], query, active),
    )


def _snap(conn, site, query: str, fetched_at: dt.datetime, features: list) -> None:
    conn.execute(
        "insert into serp_snapshots (org_id, site_id, query_norm, features, fetched_at)"
        " values (%s, %s, %s, %s, %s)",
        (site["org_id"], site["site_id"], query, Jsonb(features), fetched_at),
    )


def _rank_row(
    conn, site, query: str, day: dt.date, *, rank=None, aio_cited=False,
    aio_cited_domains=(), top=(),
) -> None:
    conn.execute(
        "insert into rank_history (org_id, site_id, query_norm, checked_on, rank,"
        " aio_present, aio_cited, aio_cited_domains, top_domains)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            site["org_id"],
            site["site_id"],
            query,
            day,
            rank,
            bool(aio_cited_domains) or aio_cited,
            aio_cited,
            list(aio_cited_domains),
            list(top),
        ),
    )


def _audit(
    conn, site, *, scores, finished: dt.datetime, gate_state=None, status="done", url=None,
) -> None:
    conn.execute(
        "insert into audits (org_id, site_id, url, registry_version, status, gate_state,"
        " scores, finished_at)"
        " values (%s, %s, %s, 'r1', %s, %s, %s, %s)",
        (site["org_id"], site["site_id"], url, status, gate_state, Jsonb(scores), finished),
    )


# --- depth cache rule (DB) --------------------------------------------------------------------


@requires_db
class TestDepthCacheRule:
    def test_depth_100_snapshot_serves_a_depth_10_request(self, conn, site):
        client, requests = make_dfs([(200, envelope([organic(1, "https://glowclinic.ae/b")]))])

        first = get_snapshot(conn, site["site_id"], "botox dubai", client=client, depth=100)
        assert first["fresh"] is True
        assert first["depth"] == 100
        assert body_of(requests[0])[0]["depth"] == 100
        assert conn.execute("select depth from serp_snapshots").fetchone()["depth"] == 100

        second = get_snapshot(conn, site["site_id"], "botox dubai", client=client, depth=10)
        assert second["fresh"] is False
        assert second["id"] == first["id"]
        assert second["depth"] == 100
        assert len(requests) == 1  # no re-buy: 100 satisfies 10

    def test_depth_10_snapshot_never_serves_a_depth_100_request(self, conn, site):
        client, requests = make_dfs([(200, envelope([organic(1, "https://glowclinic.ae/b")]))])

        first = get_snapshot(conn, site["site_id"], "botox dubai", client=client)
        assert first["depth"] == 10
        assert body_of(requests[0])[0]["depth"] == 10

        second = get_snapshot(conn, site["site_id"], "botox dubai", client=client, depth=100)
        assert second["fresh"] is True
        assert second["id"] != first["id"]
        assert len(requests) == 2
        assert body_of(requests[1])[0]["depth"] == 100

        # the deeper row now serves shallow requests too
        third = get_snapshot(conn, site["site_id"], "botox dubai", client=client, depth=10)
        assert third["fresh"] is False
        assert third["id"] == second["id"]
        assert len(requests) == 2

        events = conn.execute("select * from cost_events order by id").fetchall()
        assert len(events) == 2  # one per purchase, none for the cache hit
        assert events[0]["units"]["depth"] == 10
        assert events[1]["units"]["depth"] == 100


# --- feature share (DB) -----------------------------------------------------------------------


@requires_db
class TestFeatureShare:
    def test_weekly_buckets_attribution_and_unattributed(self, conn, site):
        _track(conn, site, "botox dubai")
        _track(conn, site, "fillers dubai")
        _track(conn, site, "laser dubai", active=False)  # not in the panel

        w1 = dt.datetime(2026, 6, 2, 12, tzinfo=dt.UTC)
        # earlier same-week snapshot for the same query: overridden by the later one
        _snap(conn, site, "botox dubai", dt.datetime(2026, 6, 1, 9, tzinfo=dt.UTC),
              [{"type": "featured_snippet", "domain": "rival.ae", "url": "https://rival.ae/x"}])
        _snap(conn, site, "botox dubai", w1, [
            {"type": "ai_overview",
             "cited_domains": ["www.glowclinic.ae", "healthline.com"]},
            {"type": "featured_snippet", "domain": "medspa-dubai.ae",
             "url": "https://medspa-dubai.ae/botox"},
            {"type": "people_also_ask", "questions": ["q"],
             "source_domains": ["blog.medspa-dubai.ae", "quora.com"]},
        ])
        # legacy snapshot (pre-retention): featured snippet without owner fields
        _snap(conn, site, "fillers dubai", dt.datetime(2026, 6, 3, 10, tzinfo=dt.UTC),
              [{"type": "featured_snippet"}])
        # second week: the client owns the snippet
        _snap(conn, site, "botox dubai", dt.datetime(2026, 6, 9, 8, tzinfo=dt.UTC),
              [{"type": "featured_snippet", "domain": "glowclinic.ae",
                "url": "https://glowclinic.ae/botox"}])
        # out of window / untracked query: both excluded
        _snap(conn, site, "botox dubai", dt.datetime(2026, 5, 20, 8, tzinfo=dt.UTC),
              [{"type": "featured_snippet", "domain": "rival.ae"}])
        _snap(conn, site, "laser dubai", w1,
              [{"type": "featured_snippet", "domain": "rival.ae"}])

        out = fs.feature_share(conn, site["site_id"], since=SINCE, until=UNTIL)
        assert out["queries"] == 2
        assert out["note"] is None
        assert [w["week_start"] for w in out["weeks"]] == ["2026-06-01", "2026-06-08"]

        week1 = out["weeks"][0]["features"]
        assert week1["featured_snippet"] == {
            "present": 2, "you": 0, "competitors": {"medspa-dubai.ae": 1},
            "other": 0, "unattributed": 1,
        }
        assert week1["ai_overview"] == {
            "present": 1, "you": 1, "competitors": {}, "other": 1, "unattributed": 0,
        }
        assert week1["people_also_ask"] == {
            "present": 1, "you": 0, "competitors": {"medspa-dubai.ae": 1},
            "other": 1, "unattributed": 0,
        }

        week2 = out["weeks"][1]["features"]
        assert week2["featured_snippet"] == {
            "present": 1, "you": 1, "competitors": {}, "other": 0, "unattributed": 0,
        }
        assert week2["ai_overview"]["present"] == 0  # measured absence, honest zero

    def test_empty_panel_note(self, conn, site):
        out = fs.feature_share(conn, site["site_id"], since=SINCE, until=UNTIL)
        assert out == {"weeks": [], "queries": 0, "note": "no tracked queries yet"}

    def test_no_snapshots_note(self, conn, site):
        _track(conn, site, "botox dubai")
        out = fs.feature_share(conn, site["site_id"], since=SINCE, until=UNTIL)
        assert out["weeks"] == []
        assert out["queries"] == 1
        assert out["note"] == "no SERP snapshots in the window yet"

    def test_all_legacy_snapshots_note_owner_retention(self, conn, site):
        _track(conn, site, "botox dubai")
        _snap(conn, site, "botox dubai", dt.datetime(2026, 6, 2, 12, tzinfo=dt.UTC),
              [{"type": "featured_snippet"}, {"type": "ai_overview"}])
        out = fs.feature_share(conn, site["site_id"], since=SINCE, until=UNTIL)
        assert out["note"] == (
            "snapshots predate feature-owner retention — owners unattributed"
        )
        features = out["weeks"][0]["features"]
        assert features["featured_snippet"]["unattributed"] == 1
        assert features["ai_overview"]["unattributed"] == 1
        assert features["featured_snippet"]["you"] == 0

    def test_featureless_snapshots_are_honest_zeros_without_note(self, conn, site):
        _track(conn, site, "botox dubai")
        _snap(conn, site, "botox dubai", dt.datetime(2026, 6, 2, 12, tzinfo=dt.UTC), [])
        out = fs.feature_share(conn, site["site_id"], since=SINCE, until=UNTIL)
        assert out["note"] is None
        assert all(
            bucket["present"] == 0 for bucket in out["weeks"][0]["features"].values()
        )


# --- competitive position (DB) ----------------------------------------------------------------


@requires_db
class TestCompetitivePosition:
    @pytest.fixture(autouse=True)
    def _no_profiles(self, monkeypatch: pytest.MonkeyPatch):
        # WP-A's module is concurrent: default every test to "module absent".
        monkeypatch.setattr(fs, "_latest_profile_fn", lambda: None)

    def test_rank_counts_from_last_in_window_rows(self, conn, site):
        _track(conn, site, "botox dubai")
        _track(conn, site, "fillers dubai")
        _rank_row(conn, site, "botox dubai", dt.date(2026, 6, 5), rank=8, top=["a.com"])
        _rank_row(
            conn, site, "botox dubai", dt.date(2026, 6, 20), rank=2, aio_cited=True,
            aio_cited_domains=["glowclinic.ae", "www.medspa-dubai.ae"],
            top=["glowclinic.ae", "www.medspa-dubai.ae", "x.com"],
        )
        _rank_row(conn, site, "fillers dubai", dt.date(2026, 6, 10), rank=None,
                  top=["rival.ae", "glowclinic.ae"])
        # outside the window: must not become the "last" row
        _rank_row(conn, site, "botox dubai", dt.date(2026, 7, 2), rank=1, top=["rival.ae"])

        out = fs.competitive_position(conn, site["site_id"], since=SINCE, until=UNTIL)
        assert out["window"] == {"since": "2026-06-01", "until": "2026-06-30"}
        assert out["note"] is None

        you = out["you"]
        assert you["domain"] == "glowclinic.ae"
        assert you["tracked_queries"] == 2
        assert you["rank_top3"] == 1
        assert you["rank_top10"] == 1
        assert you["aio_citations"] == 1
        assert you["audit_median"] is None
        assert you["audit_n"] == 0

        assert [c["domain"] for c in out["competitors"]] == ["medspa-dubai.ae", "rival.ae"]
        medspa, rival = out["competitors"]
        # subdomain-aware fingerprint position (www.medspa-dubai.ae at #2)
        assert (medspa["rank_top3"], medspa["rank_top10"]) == (1, 1)
        assert medspa["aio_citations"] == 1
        assert medspa["has_data"] is True
        # rival ranks #1 for the query the client does not rank for at all
        assert (rival["rank_top3"], rival["rank_top10"]) == (1, 1)
        assert rival["aio_citations"] == 0  # measured zero: the panel was checked
        assert rival["has_data"] is True

    def test_median_math_and_competitor_reference_gating(self, conn, site):
        _track(conn, site, "botox dubai")
        # the client's own history: draft/reference/failed/out-of-window excluded
        _audit(conn, site, scores={"overall_score": 60},
               finished=dt.datetime(2026, 6, 10, tzinfo=dt.UTC))
        _audit(conn, site, scores={"overall_score": 72},
               finished=dt.datetime(2026, 6, 20, tzinfo=dt.UTC))
        _audit(conn, site, scores={"overall_score": 1}, gate_state="draft",
               finished=dt.datetime(2026, 6, 12, tzinfo=dt.UTC))
        _audit(conn, site, scores={"overall_score": 99}, status="failed",
               finished=dt.datetime(2026, 6, 12, tzinfo=dt.UTC))
        _audit(conn, site, scores={"overall_score": 90},
               finished=dt.datetime(2026, 5, 20, tzinfo=dt.UTC))
        # competitor references: matched by url host, subdomain-aware
        _audit(conn, site, scores={"overall_score": 50}, gate_state="competitor_reference",
               url="https://www.medspa-dubai.ae/botox",
               finished=dt.datetime(2026, 6, 5, tzinfo=dt.UTC))
        _audit(conn, site, scores={"overall_score": 70}, gate_state="competitor_reference",
               url="https://medspa-dubai.ae/price",
               finished=dt.datetime(2026, 6, 15, tzinfo=dt.UTC))
        _audit(conn, site, scores={"overall_score": 65}, gate_state="competitor_reference",
               url="https://medspa-dubai.ae/x",
               finished=dt.datetime(2026, 7, 5, tzinfo=dt.UTC))  # out of window
        _audit(conn, site, scores={}, gate_state="competitor_reference",
               url="https://medspa-dubai.ae/y",
               finished=dt.datetime(2026, 6, 16, tzinfo=dt.UTC))  # no score: not counted
        # until-day audits are inside the inclusive window
        _audit(conn, site, scores={"overall_score": 80}, gate_state="competitor_reference",
               url="https://rival.ae/a",
               finished=dt.datetime(2026, 6, 30, 23, 0, tzinfo=dt.UTC))

        out = fs.competitive_position(conn, site["site_id"], since=SINCE, until=UNTIL)
        you = out["you"]
        assert (you["audit_median"], you["audit_n"]) == (66.0, 2)

        medspa, rival = out["competitors"]
        assert (medspa["audit_median"], medspa["audit_n"]) == (60.0, 2)
        assert (rival["audit_median"], rival["audit_n"]) == (80.0, 1)
        assert medspa["has_data"] is True

    def test_no_observations_is_none_never_zero(self, conn, site):
        out = fs.competitive_position(conn, site["site_id"], since=SINCE, until=UNTIL)
        assert out["note"] == "no tracked queries yet"
        you = out["you"]
        assert you["tracked_queries"] == 0
        assert you["rank_top3"] is None
        assert you["rank_top10"] is None
        assert you["aio_citations"] is None
        for comp in out["competitors"]:
            assert comp["rank_top3"] is None
            assert comp["rank_top10"] is None
            assert comp["aio_citations"] is None
            assert comp["audit_median"] is None
            assert comp["profile"] is None
            assert comp["has_data"] is False
        assert out["feature_share"]["note"] == "no tracked queries yet"

    def test_no_competitors_note_and_empty_list(self, conn, site):
        bare = conn.execute(
            "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
            (site["org_id"], "solo.ae"),
        ).fetchone()["id"]
        out = fs.competitive_position(conn, bare, since=SINCE, until=UNTIL)
        assert out["competitors"] == []
        assert out["note"] == "no tracked queries yet; no competitors configured"

    def test_profile_via_fake_accessor_flips_has_data(self, conn, site, monkeypatch):
        profile = {
            "domain": "medspa-dubai.ae", "total_keywords": 1200, "top10_keywords": 90,
            "est_traffic": 3444.4, "movers": {"new": 12, "up": 30, "down": 4, "lost": 9},
            "checked_on": "2026-06-15",
        }

        def fake_latest(_conn, _site_id, domain):
            return profile if domain == "medspa-dubai.ae" else None

        monkeypatch.setattr(fs, "_latest_profile_fn", lambda: fake_latest)
        out = fs.competitive_position(conn, site["site_id"], since=SINCE, until=UNTIL)
        medspa, rival = out["competitors"]
        assert medspa["profile"] == profile
        assert medspa["has_data"] is True  # profile alone is data
        assert medspa["rank_top3"] is None  # ...but rank counts stay honest None
        assert rival["profile"] is None
        assert rival["has_data"] is False

    def test_unknown_site_fails_fast(self, conn):
        with pytest.raises(ValueError, match="not found"):
            fs.competitive_position(conn, uuid.uuid4(), since=SINCE, until=UNTIL)


# --- receipt assembly (DB) --------------------------------------------------------------------


@requires_db
class TestReceiptAssembly:
    def test_assemble_includes_competitive_position(self, conn, site, monkeypatch):
        monkeypatch.setattr(fs, "_latest_profile_fn", lambda: None)
        _track(conn, site, "botox dubai")
        receipts.assemble_site_receipt(conn, site_id=site["site_id"], period="2026-06")
        payload = conn.execute(
            "select payload from site_deltas where site_id = %s", (site["site_id"],)
        ).fetchone()["payload"]
        position = payload["competitive_position"]
        assert position["window"] == {"since": "2026-06-01", "until": "2026-06-30"}
        assert position["you"]["domain"] == "glowclinic.ae"
        assert [c["domain"] for c in position["competitors"]] == [
            "medspa-dubai.ae", "rival.ae",
        ]
        assert all(c["has_data"] is False for c in position["competitors"])

    def test_assemble_tolerates_missing_module(self, conn, site, monkeypatch):
        monkeypatch.setattr(receipts, "_competitive_position_fn", lambda: None)
        receipts.assemble_site_receipt(conn, site_id=site["site_id"], period="2026-06")
        payload = conn.execute(
            "select payload from site_deltas where site_id = %s", (site["site_id"],)
        ).fetchone()["payload"]
        assert payload["competitive_position"] is None
        html = receipts.render_receipt_html({"domain_norm": "glowclinic.ae"}, payload)
        assert "Competitive intelligence is not available yet." in html


# --- renderer goldens (pure) ------------------------------------------------------------------


SITE = {"domain_norm": "glowclinic.ae"}


def _receipt_payload(competitive=None, *, include_key=True) -> dict:
    base = {
        "period": "2026-06",
        "prior_period": "2026-05",
        "audits": {"run": 0, "movement": {"first": None, "last": None, "change": None}},
        "fix_log": {"levers": [], "published": []},
        "content": [],
        "rank_tracking": {"available": False, "queries": [],
                          "note": "rank tracking not available yet"},
        "citations": {"prompts": [], "controls": {"sites": [], "mean_abs_drift": None}},
        "queue": {"opened": 0, "actions": []},
        "spend": {"total_cents": 0, "by_provider": []},
        "gsc": {"connected": False},
    }
    if include_key:
        base["competitive_position"] = competitive
    return base


def _position(**over) -> dict:
    base = {
        "window": {"since": "2026-06-01", "until": "2026-06-30"},
        "you": {"domain": "glowclinic.ae", "tracked_queries": 2, "rank_top3": 1,
                "rank_top10": 2, "aio_citations": 1, "audit_median": 66.0, "audit_n": 2},
        "competitors": [
            {"domain": "medspa-dubai.ae", "rank_top3": 1, "rank_top10": 2,
             "aio_citations": 1, "audit_median": 60.0, "audit_n": 2,
             "profile": {"domain": "medspa-dubai.ae", "total_keywords": 1200,
                         "top10_keywords": 90, "est_traffic": 3444.4,
                         "movers": {"new": 12, "up": 30, "down": 4, "lost": 9},
                         "checked_on": "2026-06-15"},
             "has_data": True},
            {"domain": "rival.ae", "rank_top3": None, "rank_top10": None,
             "aio_citations": None, "audit_median": None, "audit_n": 0,
             "profile": None, "has_data": False},
        ],
        "feature_share": {
            "weeks": [{"week_start": "2026-06-01", "features": {
                "ai_overview": {"present": 2, "you": 1,
                                "competitors": {"medspa-dubai.ae": 1},
                                "other": 1, "unattributed": 0},
                "featured_snippet": {"present": 1, "you": 0, "competitors": {},
                                     "other": 0, "unattributed": 1},
                "people_also_ask": {"present": 0, "you": 0, "competitors": {},
                                    "other": 0, "unattributed": 0},
            }}],
            "queries": 2,
            "note": None,
        },
        "note": None,
    }
    base.update(over)
    return base


def test_render_section_directly_after_google_visibility():
    html = receipts.render_receipt_html(SITE, _receipt_payload(_position()))
    visibility = html.index("Google visibility")
    position = html.index("Competitive position")
    citations = html.index("AI citation rates")
    assert visibility < position < citations


def test_render_position_rows_and_feature_share_table():
    html = receipts.render_receipt_html(SITE, _receipt_payload(_position()))
    assert "glowclinic.ae (you)" in html
    assert "66 (n=2)" in html  # you audit median
    assert "60 (n=2)" in html  # competitor audit median
    assert "kw 1200" in html
    assert "top10 90" in html
    assert "traffic 3444" in html
    assert "movers new 12/up 30/down 4/lost 9" in html
    # feature-share mini-table
    assert "SERP feature share" in html
    assert "2026-06-01" in html
    assert "you 1/2" in html
    assert "medspa-dubai.ae 1" in html
    assert "other 1" in html
    assert "unattributed 1" in html


def test_render_has_data_false_is_no_data_yet_never_zeros():
    html = receipts.render_receipt_html(SITE, _receipt_payload(_position()))
    row = html.split("rival.ae")[1].split("</tr>")[0]
    assert "no data yet" in row
    assert 'colspan="5"' in row
    assert ">0<" not in row  # zeros are forbidden for a no-data competitor


def test_render_section_absent_state():
    for payload in (
        _receipt_payload(include_key=False),
        _receipt_payload(None),
        _receipt_payload("nonsense"),
    ):
        html = receipts.render_receipt_html(SITE, payload)
        assert "Competitive intelligence is not available yet." in html


def test_render_notes_and_no_competitor_states():
    noted = _position(
        competitors=[], note="no tracked queries yet; no competitors configured"
    )
    html = receipts.render_receipt_html(SITE, _receipt_payload(noted))
    assert "no tracked queries yet; no competitors configured." in html
    # the note already covers it: no doubled empty-state line
    assert "No competitors configured for this site yet." not in html

    bare = _position(competitors=[], note=None)
    html = receipts.render_receipt_html(SITE, _receipt_payload(bare))
    assert "No competitors configured for this site yet." in html


def test_render_none_counts_as_mdash_not_zero():
    pos = _position()
    pos["you"].update(rank_top3=None, rank_top10=None, aio_citations=None,
                      audit_median=None, audit_n=0)
    html = receipts.render_receipt_html(SITE, _receipt_payload(pos))
    you_row = html.split("glowclinic.ae (you)")[1].split("</tr>")[0]
    assert you_row.count("&mdash;") == 5  # 4 count/median cells + profile cell
    assert ">0<" not in you_row


def test_render_feature_share_empty_states():
    pos = _position(feature_share={"weeks": [], "queries": 0,
                                   "note": "no tracked queries yet"})
    html = receipts.render_receipt_html(SITE, _receipt_payload(pos))
    assert "no tracked queries yet." in html

    pos = _position(feature_share={"weeks": [], "queries": 1, "note": None})
    html = receipts.render_receipt_html(SITE, _receipt_payload(pos))
    assert "No feature-share data in this window yet." in html


def test_render_hostile_strings_escaped_everywhere():
    hostile = "<script>alert(1)</script>"
    pos = _position()
    pos["note"] = hostile
    pos["you"]["domain"] = hostile
    pos["competitors"][0]["domain"] = hostile
    pos["feature_share"]["weeks"][0]["week_start"] = hostile
    pos["feature_share"]["weeks"][0]["features"]["ai_overview"]["competitors"] = {hostile: 1}
    html = receipts.render_receipt_html(SITE, _receipt_payload(pos))
    assert "<script" not in html
    assert "&lt;script&gt;" in html
