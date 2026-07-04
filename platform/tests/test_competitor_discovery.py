"""Competitor discovery tests (Phase D2 WP-B) — ZERO network; the labs client
is always an in-memory fake (LabsClient.competitors_domain is WP-A's file).

The pure filter/ranking helper is covered without a database. DB tests require
DATABASE_URL (standard skip guard) and the candidate-queueing ones additionally
skip when migration 010's queue_items kind 'competitor_candidate' has not
landed yet (WP-A owns that file); the integrator re-runs after 010 applies.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest

from gm.intel import discovery
from gm.intel.discovery import (
    _filter_candidates,
    confirm_candidate,
    discover_competitors,
    dismiss_candidate,
)
from gm.intel.serp import SerpError

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

CLIENT = "medspa-dubai.ae"


def cand(
    domain: str,
    *,
    intersections: int = 12,
    avg_position: float | None = 8.5,
    their_keywords: int | None = 400,
    their_etv: float | None = 1234.5,
) -> dict:
    """A competitors_domain row exactly as WP-A's contract normalizes it."""
    return {
        "domain": domain,
        "intersections": intersections,
        "avg_position": avg_position,
        "their_keywords": their_keywords,
        "their_etv": their_etv,
    }


class FakeLabs:
    """In-memory stand-in for LabsClient: fixed rows, per-call cost, call log."""

    def __init__(self, rows: list[dict], cost_cents: float = 1.2):
        self.rows = rows
        self.cost_cents = cost_cents
        self.calls: list[dict] = []
        self.last_cost_cents = 0.0

    def competitors_domain(self, domain, *, location_code=2784, language="en", limit=30):
        self.calls.append(
            {"domain": domain, "location_code": location_code,
             "language": language, "limit": limit}
        )
        self.last_cost_cents = self.cost_cents
        return [dict(r) for r in self.rows]


# --- filter matrix (pure, no DB) --------------------------------------------------------


def _hosts(rows, **kwargs):
    return [r["domain"] for r in _filter_candidates(rows, **kwargs)]


def test_filter_excludes_client_subdomain_aware():
    rows = [
        cand(CLIENT),                    # the client itself
        cand("blog." + CLIENT),          # client subdomain
        cand("www." + CLIENT),           # normalizes to the client
        cand("rival.ae"),
    ]
    assert _hosts(rows, client_host=CLIENT, configured=[]) == ["rival.ae"]
    # both directions: candidate that is a PARENT of the client host is out too
    assert _hosts(
        [cand(CLIENT), cand("rival.ae")], client_host="blog." + CLIENT, configured=[]
    ) == ["rival.ae"]


def test_filter_excludes_configured_domains_normalized():
    rows = [cand("known.ae"), cand("other.ae"), cand("fresh.ae")]
    kept = _hosts(rows, client_host=CLIENT, configured=["known.ae", "https://WWW.Other.AE/x"])
    assert kept == ["fresh.ae"]


def test_filter_excludes_denylist_subdomain_aware():
    rows = [
        cand("instagram.com"),           # denylisted platform
        cand("business.instagram.com"),  # subdomain of one
        cand("rival.ae"),
    ]
    assert _hosts(rows, client_host=CLIENT, configured=[]) == ["rival.ae"]


def test_filter_intersections_floor_inclusive():
    rows = [
        cand("weak.ae", intersections=2),      # below floor
        cand("edge.ae", intersections=3),      # boundary: >= 3 stays
        cand("strong.ae", intersections=40),
    ]
    assert _hosts(rows, client_host=CLIENT, configured=[]) == ["strong.ae", "edge.ae"]


def test_filter_tolerates_junk_rows():
    rows = [
        "not-a-dict",
        {"intersections": 9},                          # no domain
        cand("", intersections=9),                     # empty domain
        cand("nullish.ae", intersections=None),        # unknown overlap: no evidence
        {"domain": "boolish.ae", "intersections": True},  # bool is not a count
        cand("dupe.ae", intersections=5),
        cand("dupe.ae", intersections=50),             # dedupe: first occurrence wins
        cand("ok.ae", intersections=7),
    ]
    assert _hosts(rows, client_host=CLIENT, configured=[]) == ["ok.ae", "dupe.ae"]


def test_filter_ordering_ties_and_limit():
    rows = [
        cand("low.ae", intersections=4, avg_position=1.0),
        cand("b-tie.ae", intersections=9, avg_position=5.0),
        cand("a-tie.ae", intersections=9, avg_position=5.0),   # position tie -> domain asc
        cand("best-pos.ae", intersections=9, avg_position=2.0),
        cand("no-pos.ae", intersections=9, avg_position=None),  # unknown position sorts last
        cand("top.ae", intersections=30, avg_position=9.9),
    ]
    ranked = _hosts(rows, client_host=CLIENT, configured=[])
    assert ranked == ["top.ae", "best-pos.ae", "a-tie.ae", "b-tie.ae", "no-pos.ae", "low.ae"]
    assert _hosts(rows, client_host=CLIENT, configured=[], limit=2) == ["top.ae", "best-pos.ae"]


# --- DB fixtures -------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    from gm import db

    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    from gm import db

    with db.connect(autocommit=True) as c:
        c.execute("truncate queue_items cascade")
        c.execute("truncate cost_events restart identity")
        yield c


def _candidate_kind_allowed(conn) -> bool:
    row = conn.execute(
        "select pg_get_constraintdef(oid) as condef from pg_constraint"
        " where conname = 'queue_items_kind_check'"
    ).fetchone()
    return row is None or "competitor_candidate" in row["condef"]


@pytest.fixture()
def candidate_conn(conn):
    if not _candidate_kind_allowed(conn):
        pytest.skip(
            "migration 010 not applied yet: queue_items kind 'competitor_candidate' missing"
        )
    return conn


def make_site(conn, competitors: list[str]):
    org_id = conn.execute(
        "insert into orgs (name) values (%s) returning id", (f"disc-{uuid.uuid4().hex[:8]}",)
    ).fetchone()["id"]
    site_id = conn.execute(
        "insert into sites (org_id, domain_norm, competitor_domains)"
        " values (%s, %s, %s) returning id",
        (org_id, f"client-{uuid.uuid4().hex[:8]}.ae", competitors),
    ).fetchone()["id"]
    return {"org_id": org_id, "site_id": site_id}


def _items(conn, site_id):
    return conn.execute(
        "select * from queue_items where site_id = %s and kind = 'competitor_candidate'"
        " order by at_stake->>'intersections' desc, target->>'domain'",
        (site_id,),
    ).fetchall()


def _configured(conn, site_id) -> list[str]:
    return conn.execute(
        "select competitor_domains from sites where id = %s", (site_id,)
    ).fetchone()["competitor_domains"]


def _run(conn, site, fake, **kwargs):
    return discover_competitors(
        conn, org_id=site["org_id"], site_id=site["site_id"], labs_client=fake, **kwargs
    )


def _domain_of(site, conn):
    return conn.execute(
        "select domain_norm from sites where id = %s", (site["site_id"],)
    ).fetchone()["domain_norm"]


# --- discovery guards that never buy or queue (DB, no 010 needed) -------------------------


@requires_db
def test_limit_over_max_refused_zero_spend(conn):
    site = make_site(conn, [])
    fake = FakeLabs([cand("rival.ae")])
    result = _run(conn, site, fake, limit=discovery.MAX_COMPETITORS + 1)
    assert result["candidates"] == result["queued"] == 0
    assert result["cost_cents"] == 0.0
    assert "MAX_COMPETITORS" in result["note"]
    assert fake.calls == []  # refused BEFORE spending
    assert _items(conn, site["site_id"]) == []
    assert conn.execute("select count(*) as n from cost_events").fetchone()["n"] == 0


@requires_db
def test_limit_below_one_refused_zero_spend(conn):
    site = make_site(conn, [])
    fake = FakeLabs([cand("rival.ae")])
    result = _run(conn, site, fake, limit=0)
    assert result["queued"] == 0
    assert result["cost_cents"] == 0.0
    assert "at least 1" in result["note"]
    assert fake.calls == []
    assert conn.execute("select count(*) as n from cost_events").fetchone()["n"] == 0


@requires_db
def test_unknown_site_raises(conn):
    with pytest.raises(SerpError):
        discover_competitors(
            conn, org_id=uuid.uuid4(), site_id=uuid.uuid4(), labs_client=FakeLabs([])
        )


@requires_db
def test_empty_provider_result_honest_note_cost_still_recorded(conn):
    site = make_site(conn, [])
    fake = FakeLabs([], cost_cents=2.03)
    result = _run(conn, site, fake)
    assert result["candidates"] == result["queued"] == 0
    assert "no competitor data" in result["note"]
    assert result["cost_cents"] == pytest.approx(2.03)
    events = conn.execute("select * from cost_events").fetchall()
    assert len(events) == 1  # we paid for the call even though it found nothing
    assert events[0]["provider"] == "dataforseo"
    assert events[0]["purpose"] == "labs_competitors_domain"
    assert float(events[0]["cost_cents"]) == pytest.approx(2.03)
    assert events[0]["units"] == {"target": _domain_of(site, conn), "rows": 0}


@requires_db
def test_all_rows_filtered_honest_note_cost_recorded(conn):
    site = make_site(conn, ["known.ae"])
    fake = FakeLabs(
        [cand("instagram.com"), cand("known.ae"), cand("thin.ae", intersections=1)],
        cost_cents=1.5,
    )
    result = _run(conn, site, fake)
    assert result["candidates"] == result["queued"] == 0
    assert "no candidates survived" in result["note"]
    assert result["cost_cents"] == pytest.approx(1.5)
    events = conn.execute("select * from cost_events").fetchall()
    assert len(events) == 1
    assert events[0]["units"]["rows"] == 3
    assert _items(conn, site["site_id"]) == []


# --- discovery queueing (DB, needs 010's queue kind) --------------------------------------


@requires_db
def test_discover_filters_and_queues_candidates(candidate_conn):
    conn = candidate_conn
    site = make_site(conn, ["known.ae"])
    client_host = _domain_of(site, conn)
    fake = FakeLabs(
        [
            cand(client_host),                          # the client itself
            cand("blog." + client_host),                # client subdomain
            cand("known.ae"),                           # already configured
            cand("instagram.com"),                      # denylisted
            cand("thin.ae", intersections=2),           # below the overlap floor
            cand("rival.ae", intersections=12, avg_position=8.5,
                 their_keywords=400, their_etv=1234.5),
            cand("upstart.ae", intersections=3, avg_position=None,
                 their_keywords=None, their_etv=None),
        ],
        cost_cents=1.21,
    )
    result = _run(conn, site, fake)

    assert fake.calls == [
        {"domain": client_host, "location_code": 2784, "language": "en", "limit": 30}
    ]
    assert result == {"candidates": 2, "queued": 2, "cost_cents": pytest.approx(1.21),
                      "note": None}

    from gm.intel import detectors

    items = _items(conn, site["site_id"])
    by_host = {r["target"]["domain"]: r for r in items}
    assert set(by_host) == {"rival.ae", "upstart.ae"}
    rival = by_host["rival.ae"]
    assert rival["status"] == "open"
    assert rival["target_hash"] == detectors.target_hash({"domain": "rival.ae"})
    assert rival["at_stake"] == {
        "intersections": 12, "avg_position": 8.5, "their_keywords": 400,
        "their_etv": 1234.5, "basis": "labs",
    }
    # honest absence: unknown provider metrics stay null, never fake zeros
    assert by_host["upstart.ae"]["at_stake"] == {
        "intersections": 3, "avg_position": None, "their_keywords": None,
        "their_etv": None, "basis": "labs",
    }


@requires_db
def test_discover_limit_caps_by_intersections(candidate_conn):
    conn = candidate_conn
    site = make_site(conn, [])
    fake = FakeLabs(
        [
            cand("fourth.ae", intersections=4),
            cand("first.ae", intersections=20),
            cand("second.ae", intersections=15, avg_position=3.0),
            cand("third.ae", intersections=15, avg_position=9.0),
        ]
    )
    result = _run(conn, site, fake, limit=2)
    assert result["candidates"] == 4  # survivors counted honestly...
    assert result["queued"] == 2      # ...but only the top `limit` queued
    queued = {r["target"]["domain"] for r in _items(conn, site["site_id"])}
    assert queued == {"first.ae", "second.ae"}


@requires_db
def test_queue_discipline_including_dismiss_snooze_reruns(candidate_conn):
    conn = candidate_conn
    site = make_site(conn, [])
    sid = site["site_id"]

    _run(conn, site, FakeLabs([cand("rival.ae", intersections=10)]))
    first = _items(conn, sid)[0]
    assert first["status"] == "open"

    # open rows refresh at_stake, keep identity + first_seen
    _run(conn, site, FakeLabs([cand("rival.ae", intersections=11)]))
    rows = _items(conn, sid)
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"]
    assert rows[0]["at_stake"]["intersections"] == 11
    assert rows[0]["first_seen"] == first["first_seen"]

    # dismissed with a live snooze: re-runs leave it alone
    assert dismiss_candidate(conn, site_id=sid, domain="rival.ae", snooze_days=30) is True
    _run(conn, site, FakeLabs([cand("rival.ae", intersections=12)]))
    row = _items(conn, sid)[0]
    assert row["status"] == "dismissed"
    assert row["at_stake"]["intersections"] == 11

    # elapsed snooze: the next run reopens with fresh at_stake
    conn.execute(
        "update queue_items set snooze_until = now() - interval '1 second' where site_id = %s",
        (sid,),
    )
    _run(conn, site, FakeLabs([cand("rival.ae", intersections=12)]))
    row = _items(conn, sid)[0]
    assert row["status"] == "open"
    assert row["snooze_until"] is None
    assert row["at_stake"]["intersections"] == 12

    # actioned rows are never touched
    conn.execute("update queue_items set status = 'actioned' where site_id = %s", (sid,))
    _run(conn, site, FakeLabs([cand("rival.ae", intersections=99)]))
    row = _items(conn, sid)[0]
    assert row["status"] == "actioned"
    assert row["at_stake"]["intersections"] == 12


# --- confirm / dismiss ---------------------------------------------------------------------


@requires_db
def test_confirm_appends_dedupes_and_actions_row(candidate_conn):
    conn = candidate_conn
    site = make_site(conn, [])
    sid = site["site_id"]
    _run(conn, site, FakeLabs([cand("rival.ae")]))

    assert confirm_candidate(conn, site_id=sid, domain="RIVAL.AE") is True  # normalized
    assert _configured(conn, sid) == ["rival.ae"]
    row = _items(conn, sid)[0]
    assert row["status"] == "actioned"
    assert row["snooze_until"] is None

    # dedupe: confirming again neither duplicates nor errors
    assert confirm_candidate(conn, site_id=sid, domain="rival.ae") is False
    assert _configured(conn, sid) == ["rival.ae"]

    # a re-run now filters the confirmed domain (configured) and leaves the row actioned
    result = _run(conn, site, FakeLabs([cand("rival.ae", intersections=50)]))
    assert result["queued"] == 0
    row = _items(conn, sid)[0]
    assert row["status"] == "actioned"
    assert row["at_stake"]["intersections"] == 12


@requires_db
def test_confirm_without_candidate_row_still_appends(conn):
    # hand-picking stays legal: no queue row is required (and none is created)
    site = make_site(conn, ["existing.ae"])
    sid = site["site_id"]
    assert confirm_candidate(conn, site_id=sid, domain="https://WWW.New-One.AE/path") is True
    assert _configured(conn, sid) == ["existing.ae", "new-one.ae"]
    assert confirm_candidate(conn, site_id=sid, domain="new-one.ae") is False
    assert confirm_candidate(conn, site_id=sid, domain="www.existing.ae") is False
    assert _configured(conn, sid) == ["existing.ae", "new-one.ae"]
    assert _items(conn, sid) == []


@requires_db
def test_confirm_unknown_site_raises(conn):
    with pytest.raises(SerpError):
        confirm_candidate(conn, site_id=uuid.uuid4(), domain="rival.ae")


@requires_db
def test_dismiss_sets_snooze_days(candidate_conn):
    conn = candidate_conn
    site = make_site(conn, [])
    sid = site["site_id"]
    _run(conn, site, FakeLabs([cand("rival.ae")]))

    assert dismiss_candidate(conn, site_id=sid, domain="rival.ae") is True
    row = _items(conn, sid)[0]
    assert row["status"] == "dismissed"
    now = conn.execute("select now() as t").fetchone()["t"]
    assert dt.timedelta(days=89) < (row["snooze_until"] - now) <= dt.timedelta(days=90)

    # custom snooze re-dismisses (still dismissible) with the new horizon
    assert dismiss_candidate(conn, site_id=sid, domain="rival.ae", snooze_days=5) is True
    row = _items(conn, sid)[0]
    now = conn.execute("select now() as t").fetchone()["t"]
    assert dt.timedelta(days=4) < (row["snooze_until"] - now) <= dt.timedelta(days=5)


@requires_db
def test_dismiss_without_candidate_row_returns_false(conn):
    site = make_site(conn, [])
    assert dismiss_candidate(conn, site_id=site["site_id"], domain="ghost.ae") is False


@requires_db
def test_dismiss_never_touches_actioned(candidate_conn):
    conn = candidate_conn
    site = make_site(conn, [])
    sid = site["site_id"]
    _run(conn, site, FakeLabs([cand("rival.ae")]))
    conn.execute("update queue_items set status = 'actioned' where site_id = %s", (sid,))
    assert dismiss_candidate(conn, site_id=sid, domain="rival.ae") is False
    assert _items(conn, sid)[0]["status"] == "actioned"


# --- job handler ----------------------------------------------------------------------------


def _job(site, site_id=..., org_id=..., payload=None):
    from gm.infra import jobs

    now = dt.datetime.now(dt.UTC)
    return jobs.JobRow(
        id=1, type="discover_competitors",
        org_id=site["org_id"] if org_id is ... else org_id,
        site_id=site["site_id"] if site_id is ... else site_id,
        payload=payload or {}, status="running", priority=5, run_after=now, attempts=1,
        max_attempts=3, idempotency_key=None, concurrency_key=None, locked_by="w",
        locked_until=None, last_error=None, created_at=now, finished_at=None,
    )


@requires_db
def test_handle_discover_competitors_payload_limit(candidate_conn, monkeypatch):
    from gm.infra import jobs

    conn = candidate_conn
    site = make_site(conn, [])
    fake = FakeLabs([cand("first.ae", intersections=9), cand("second.ae", intersections=4)])
    monkeypatch.setattr(discovery, "LabsClient", lambda: fake)

    ctx = jobs.JobContext(_job(site, payload={"limit": 1}), conn, "w", 60)
    discovery.handle_discover_competitors(ctx)
    items = _items(conn, site["site_id"])
    assert [r["target"]["domain"] for r in items] == ["first.ae"]


@requires_db
def test_handle_resolves_org_from_site(conn, monkeypatch):
    from gm.infra import jobs

    site = make_site(conn, [])
    fake = FakeLabs([])  # empty provider result: no queue writes, works pre-010
    monkeypatch.setattr(discovery, "LabsClient", lambda: fake)

    discovery.handle_discover_competitors(jobs.JobContext(_job(site, org_id=None), conn, "w", 60))
    assert len(fake.calls) == 1
    event = conn.execute("select org_id from cost_events").fetchone()
    assert event["org_id"] == site["org_id"]


@requires_db
def test_handle_requires_site(conn):
    from gm.infra import jobs

    site = make_site(conn, [])
    with pytest.raises(RuntimeError, match="site_id"):
        discovery.handle_discover_competitors(
            jobs.JobContext(_job(site, site_id=None), conn, "w", 60)
        )
