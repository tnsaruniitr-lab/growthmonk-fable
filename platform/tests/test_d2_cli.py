"""Phase D2 WP-D tests: `gm competitors` CLI, track-depth plumbing, worker
registration, and the /admin competitive-position route.

ZERO network — the labs/serp clients are in-memory fakes (LabsClient is
monkeypatched where a command would construct one). CLI commands run through
typer's CliRunner against the real database; `cli._org` is monkeypatched to
the per-test org so the single-org assumption of `gm` never trips over rows
other test files create. DB tests skip without DATABASE_URL.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from typer.testing import CliRunner

from gm import api, cli
from gm.intel import rank_tracker
from gm.intel.serp import SerpResult

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

runner = CliRunner()
client = TestClient(api.app)


# --- fakes -------------------------------------------------------------------------------


class FakeSerp:
    """serp.get_snapshot's client port: records the depth of every purchase."""

    def __init__(self):
        self.calls: list[dict] = []
        self.last_cost_cents = 0.0

    def serp_live(self, query, *, location="United Arab Emirates", language="en", depth=10):
        self.calls.append({"query": query, "depth": depth})
        self.last_cost_cents = 0.2
        return SerpResult(
            query=query,
            location=location,
            organic=[
                {
                    "rank": 1,
                    "url": "https://someone-else.ae/",
                    "domain": "someone-else.ae",
                    "title": "t",
                    "description": "",
                    "type": "organic",
                }
            ],
            features=[],
            cost_cents=0.2,
        )


class FakeLabs:
    """LabsClient stand-in for the discovery inline path (zero network)."""

    def __init__(self, rows: list[dict] | None = None, cost_cents: float = 1.2):
        self.rows = rows or []
        self.cost_cents = cost_cents
        self.calls: list[str] = []
        self.last_cost_cents = 0.0

    def competitors_domain(self, domain, *, location_code=2784, language="en", limit=30):
        self.calls.append(domain)
        self.last_cost_cents = self.cost_cents
        return [dict(r) for r in self.rows]


# --- DB fixtures -------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    from gm import db

    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    from gm import db

    with db.connect(autocommit=True) as c:
        yield c


@pytest.fixture()
def site(conn, monkeypatch):
    """Fresh org+site; `gm` commands resolve to this org (patched _org)."""
    org_id = conn.execute(
        "insert into orgs (name) values (%s) returning id", (f"wpd-{uuid.uuid4().hex[:8]}",)
    ).fetchone()["id"]
    domain = f"client-{uuid.uuid4().hex[:8]}.ae"
    site_id = conn.execute(
        "insert into sites (org_id, domain_norm, competitor_domains)"
        " values (%s, %s, %s) returning id",
        (org_id, domain, []),
    ).fetchone()["id"]
    monkeypatch.setattr(cli, "_org", lambda _conn: {"id": org_id, "name": "wpd"})
    return {"org_id": org_id, "site_id": site_id, "domain": domain}


def queue_candidate(conn, site, host, intersections=9):
    from gm.intel.detectors import _upsert_item

    _upsert_item(
        conn,
        org_id=site["org_id"],
        site_id=site["site_id"],
        kind="competitor_candidate",
        target={"domain": host},
        at_stake={"intersections": intersections, "basis": "labs"},
    )


def tracked_rows(conn, site):
    return conn.execute(
        "select query_norm, serp_depth, active from tracked_queries where site_id = %s"
        " order by query_norm",
        (site["site_id"],),
    ).fetchall()


# --- track --depth / set-depth ------------------------------------------------------------


def test_track_depth_flag_validated_before_any_work():
    # Rejected by the CLI flag check itself — no DB, no site lookup needed.
    result = runner.invoke(cli.app, ["track", "add", "x.ae", "q", "--depth", "50"])
    assert result.exit_code != 0
    assert "10 or 100" in result.output
    result = runner.invoke(cli.app, ["track", "set-depth", "x.ae", "q", "--depth", "11"])
    assert result.exit_code != 0
    assert "10 or 100" in result.output


def test_add_tracked_query_rejects_bad_depth_value():
    with pytest.raises(ValueError, match="10, 100"):
        rank_tracker._check_depth(50)
    with pytest.raises(ValueError):
        rank_tracker._check_depth(True)  # bool is not a depth


@requires_db
def test_track_add_depth_writes_serp_depth(conn, site):
    result = runner.invoke(
        cli.app, ["track", "add", site["domain"], "Botox  Dubai", "--depth", "100"]
    )
    assert result.exit_code == 0, result.output
    rows = tracked_rows(conn, site)
    assert [(r["query_norm"], r["serp_depth"]) for r in rows] == [("botox dubai", 100)]

    # no flag -> the default depth 10
    result = runner.invoke(cli.app, ["track", "add", site["domain"], "fillers dubai"])
    assert result.exit_code == 0, result.output
    rows = tracked_rows(conn, site)
    assert [(r["query_norm"], r["serp_depth"]) for r in rows] == [
        ("botox dubai", 100),
        ("fillers dubai", 10),
    ]

    # re-add WITHOUT --depth never clobbers an opted-in 100
    result = runner.invoke(cli.app, ["track", "add", site["domain"], "botox dubai"])
    assert result.exit_code == 0, result.output
    assert tracked_rows(conn, site)[0]["serp_depth"] == 100


@requires_db
def test_track_set_depth_updates_existing_query_only(conn, site):
    runner.invoke(cli.app, ["track", "add", site["domain"], "fillers dubai"])
    result = runner.invoke(
        cli.app, ["track", "set-depth", site["domain"], "Fillers  DUBAI", "--depth", "100"]
    )
    assert result.exit_code == 0, result.output
    assert tracked_rows(conn, site)[0]["serp_depth"] == 100

    result = runner.invoke(
        cli.app, ["track", "set-depth", site["domain"], "never tracked", "--depth", "100"]
    )
    assert result.exit_code == 1
    assert "not tracked" in result.output
    assert len(tracked_rows(conn, site)) == 1  # set-depth never registers new queries


@requires_db
def test_track_site_passes_per_query_depth_to_snapshots(conn, site):
    rank_tracker.add_tracked_query(conn, site["org_id"], site["site_id"], "shallow query")
    rank_tracker.add_tracked_query(
        conn, site["org_id"], site["site_id"], "deep query", serp_depth=100
    )
    fake = FakeSerp()
    result = rank_tracker.track_site(
        conn, org_id=site["org_id"], site_id=site["site_id"], serp_client=fake
    )
    assert result["tracked"] == 2 and result["fresh"] == 2 and result["errors"] == 0
    assert {c["query"]: c["depth"] for c in fake.calls} == {
        "shallow query": 10,
        "deep query": 100,
    }
    snaps = conn.execute(
        "select query_norm, depth from serp_snapshots where site_id = %s order by query_norm",
        (site["site_id"],),
    ).fetchall()
    assert [(s["query_norm"], s["depth"]) for s in snaps] == [
        ("deep query", 100),
        ("shallow query", 10),
    ]
    # rank NULL at any depth when absent — honest, never 0
    ranks = conn.execute(
        "select rank from rank_history where site_id = %s", (site["site_id"],)
    ).fetchall()
    assert [r["rank"] for r in ranks] == [None, None]

    # same-week re-run: the depth-satisfying cache serves both, nothing re-bought
    rerun = rank_tracker.track_site(
        conn, org_id=site["org_id"], site_id=site["site_id"], serp_client=fake
    )
    assert rerun["cached"] == 2 and rerun["fresh"] == 0
    assert len(fake.calls) == 2


# --- competitors confirm / dismiss round-trip ----------------------------------------------


@requires_db
def test_confirm_dismiss_round_trip(conn, site):
    queue_candidate(conn, site, "rival.ae")
    queue_candidate(conn, site, "noise.ae")

    result = runner.invoke(cli.app, ["competitors", "confirm", site["domain"], "rival.ae"])
    assert result.exit_code == 0, result.output
    assert "added to competitor_domains" in result.output
    configured = conn.execute(
        "select competitor_domains from sites where id = %s", (site["site_id"],)
    ).fetchone()["competitor_domains"]
    assert configured == ["rival.ae"]
    statuses = {
        r["target"]["domain"]: r["status"]
        for r in conn.execute(
            "select target, status from queue_items where site_id = %s", (site["site_id"],)
        ).fetchall()
    }
    assert statuses == {"rival.ae": "actioned", "noise.ae": "open"}

    # confirm again: dedupe — still one entry, honest message, exit 0
    result = runner.invoke(cli.app, ["competitors", "confirm", site["domain"], "rival.ae"])
    assert result.exit_code == 0
    assert "already configured" in result.output
    assert conn.execute(
        "select competitor_domains from sites where id = %s", (site["site_id"],)
    ).fetchone()["competitor_domains"] == ["rival.ae"]

    result = runner.invoke(
        cli.app, ["competitors", "dismiss", site["domain"], "noise.ae", "--snooze-days", "30"]
    )
    assert result.exit_code == 0, result.output
    assert "snoozed 30d" in result.output
    row = conn.execute(
        "select status, snooze_until from queue_items where site_id = %s"
        " and target->>'domain' = 'noise.ae'",
        (site["site_id"],),
    ).fetchone()
    assert row["status"] == "dismissed"
    assert row["snooze_until"] is not None

    # actioned rows are never dismissible; a ghost host has nothing to dismiss
    for host in ("rival.ae", "ghost.ae"):
        result = runner.invoke(cli.app, ["competitors", "dismiss", site["domain"], host])
        assert result.exit_code == 1
        assert "no open candidate" in result.output


# --- competitors refresh / discover job + schedule plumbing --------------------------------


@requires_db
def test_refresh_monthly_schedules_row_shape(conn, site):
    result = runner.invoke(cli.app, ["competitors", "refresh", site["domain"], "--monthly"])
    assert result.exit_code == 0, result.output
    rows = conn.execute(
        "select * from schedules where site_id = %s", (site["site_id"],)
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["job_type"] == "refresh_competitor_profiles"
    assert row["every_minutes"] == 43200
    assert row["payload"] == {}
    assert row["org_id"] == site["org_id"]
    assert row["enabled"] is True

    # repeat --monthly: no duplicate schedule
    result = runner.invoke(cli.app, ["competitors", "refresh", site["domain"], "--monthly"])
    assert "already scheduled" in result.output
    n = conn.execute(
        "select count(*) as n from schedules where site_id = %s", (site["site_id"],)
    ).fetchone()["n"]
    assert n == 1
    # --monthly is the schedule path only: no one-off job was enqueued
    jobs_n = conn.execute(
        "select count(*) as n from jobs where site_id = %s", (site["site_id"],)
    ).fetchone()["n"]
    assert jobs_n == 0


@requires_db
def test_refresh_default_enqueues_job(conn, site):
    result = runner.invoke(cli.app, ["competitors", "refresh", site["domain"]])
    assert result.exit_code == 0, result.output
    assert "enqueued job" in result.output
    job = conn.execute(
        "select type, payload, org_id from jobs where site_id = %s", (site["site_id"],)
    ).fetchone()
    assert job["type"] == "refresh_competitor_profiles"
    assert job["payload"] == {}
    assert job["org_id"] == site["org_id"]


@requires_db
def test_refresh_now_empty_config_honest_note_zero_spend(conn, site):
    # No competitor_domains configured: the WP-A empty-state note verbatim, no client built.
    result = runner.invoke(cli.app, ["competitors", "refresh", site["domain"], "--now"])
    assert result.exit_code == 0, result.output
    assert "refreshed=0 cached=0 empty=0 cost=$0.0000" in result.output
    assert "no competitor_domains configured" in result.output


@requires_db
def test_discover_default_enqueues_job(conn, site):
    result = runner.invoke(
        cli.app, ["competitors", "discover", site["domain"], "--limit", "5"]
    )
    assert result.exit_code == 0, result.output
    assert "enqueued job" in result.output
    job = conn.execute(
        "select type, payload from jobs where site_id = %s", (site["site_id"],)
    ).fetchone()
    assert job["type"] == "discover_competitors"
    assert job["payload"] == {"site_id": str(site["site_id"]), "limit": 5}


@requires_db
def test_discover_now_runs_inline_with_note(conn, site, monkeypatch):
    fake = FakeLabs([], cost_cents=1.2)
    monkeypatch.setattr("gm.intel.discovery.LabsClient", lambda: fake)
    result = runner.invoke(cli.app, ["competitors", "discover", site["domain"], "--now"])
    assert result.exit_code == 0, result.output
    assert fake.calls == [site["domain"]]
    assert "candidates=0 queued=0" in result.output
    assert f"note: provider returned no competitor data for {site['domain']}" in result.output


# --- competitors list ----------------------------------------------------------------------


@requires_db
def test_list_profiles_empty_states_and_candidate_count(conn, site):
    result = runner.invoke(cli.app, ["competitors", "list", site["domain"]])
    assert result.exit_code == 0, result.output
    assert "no competitors configured" in result.output
    assert "open candidates: 0" in result.output

    conn.execute(
        "update sites set competitor_domains = %s where id = %s",
        (["rival.ae", "fresh.ae"], site["site_id"]),
    )
    conn.execute(
        "insert into competitor_profiles (org_id, site_id, domain, checked_on,"
        " total_keywords, top10_keywords, est_traffic, movers)"
        " values (%s, %s, 'rival.ae', current_date, 1200, 88, 4321.5, %s)",
        (site["org_id"], site["site_id"], Jsonb({"new": 5, "up": 2, "down": 1, "lost": 0})),
    )
    queue_candidate(conn, site, "upstart.ae")

    result = runner.invoke(cli.app, ["competitors", "list", site["domain"]])
    assert result.exit_code == 0, result.output
    assert "kw=1200 top10=88 traffic=4322" in result.output
    assert "new=5 up=2 down=1 lost=0" in result.output
    assert "no data yet" in result.output  # fresh.ae was never fetched
    assert "open candidates: 1" in result.output


# --- competitors position ------------------------------------------------------------------


@requires_db
def test_position_renders_empty_states_verbatim(conn, site):
    conn.execute(
        "update sites set competitor_domains = %s where id = %s",
        (["rival.ae"], site["site_id"]),
    )
    result = runner.invoke(
        cli.app, ["competitors", "position", site["domain"], "--month", "2026-07"]
    )
    assert result.exit_code == 0, result.output
    assert "window 2026-07-01 → 2026-07-31" in result.output
    assert "note: no tracked queries yet" in result.output  # position note verbatim
    assert f"you: {site['domain']}  tracked=0" in result.output
    assert "top3=— top10=— aio=—" in result.output  # None renders as dash, never 0
    assert "rival.ae: no data yet" in result.output  # has_data=False law
    assert "feature share (0 tracked queries):" in result.output


def test_position_lines_pure_rendering_laws():
    position = {
        "window": {"since": "2026-07-01", "until": "2026-07-31"},
        "you": {
            "domain": "client.ae", "tracked_queries": 2, "rank_top3": 1, "rank_top10": 2,
            "aio_citations": 0, "audit_median": 81.5, "audit_n": 3,
        },
        "competitors": [
            {"domain": "rival.ae", "rank_top3": 0, "rank_top10": 2, "aio_citations": 1,
             "audit_median": 70.0, "audit_n": 1,
             "profile": {"domain": "rival.ae", "total_keywords": 10, "top10_keywords": None,
                         "est_traffic": None, "movers": {}, "checked_on": dt.date(2026, 7, 1)},
             "has_data": True},
            {"domain": "ghost.ae", "rank_top3": None, "rank_top10": None,
             "aio_citations": None, "audit_median": None, "audit_n": 0,
             "profile": None, "has_data": False},
        ],
        "feature_share": {
            "weeks": [
                {"week_start": "2026-06-29",
                 "features": {"ai_overview": {"present": 2, "you": 1,
                                              "competitors": {"rival.ae": 1},
                                              "other": 1, "unattributed": 0},
                              "featured_snippet": {"present": 0, "you": 0, "competitors": {},
                                                   "other": 0, "unattributed": 0},
                              "people_also_ask": {"present": 1, "you": 0, "competitors": {},
                                                  "other": 0, "unattributed": 1}}}
            ],
            "queries": 2,
            "note": None,
        },
        "note": None,
    }
    text = "\n".join(cli._position_lines(position))
    assert "you: client.ae  tracked=2 top3=1 top10=2 aio=0 audit=81.5 (n=3)" in text
    assert "rival.ae: top3=0 top10=2 aio=1 audit=70.0 (n=1)" in text
    assert "profile: kw=10 top10=— traffic=— (2026-07-01)" in text
    assert "ghost.ae: no data yet" in text
    assert "top3=—" not in text.replace("ghost.ae: no data yet", "")  # dashes only via None
    assert "2026-06-29 ai_overview: present=2 you=1 rival.ae=1 other=1 unattributed=0" in text
    assert "featured_snippet" not in text  # present=0 weeks-rows are skipped
    assert "2026-06-29 people_also_ask: present=1 you=0 other=0 unattributed=1" in text


# --- worker handler registration ------------------------------------------------------------


def test_worker_registers_d2_handlers_lazily(monkeypatch):
    captured: dict = {}

    class FakeWorker:
        def __init__(self, handlers, **kwargs):
            captured.update(handlers)

        def run_forever(self, stop_event=None):
            return None

    monkeypatch.setattr(cli.jobs_mod, "Worker", FakeWorker)
    result = runner.invoke(cli.app, ["worker"])
    assert result.exit_code == 0, result.output
    assert "refresh_competitor_profiles" in captured
    assert "discover_competitors" in captured
    # pre-D2 handlers still registered
    assert {"track_serps", "keyword_gap", "send_lead_card"} <= set(captured)

    # the wrappers import lazily and dispatch to the real handlers at call time
    calls: list[tuple] = []
    monkeypatch.setattr(
        "gm.intel.competitors.handle_refresh_competitor_profiles",
        lambda ctx: calls.append(("refresh", ctx)),
    )
    monkeypatch.setattr(
        "gm.intel.discovery.handle_discover_competitors",
        lambda ctx: calls.append(("discover", ctx)),
    )
    sentinel = object()
    captured["refresh_competitor_profiles"](sentinel)
    captured["discover_competitors"](sentinel)
    assert calls == [("refresh", sentinel), ("discover", sentinel)]


# --- admin competitive-position route --------------------------------------------------------


def test_admin_competitors_guard(monkeypatch):
    url = f"/admin/sites/{uuid.uuid4()}/competitors"
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    assert client.get(url, headers={"X-Admin-Token": "anything"}).status_code == 404
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    assert client.get(url).status_code == 404
    assert client.get(url, headers={"X-Admin-Token": "nope"}).status_code == 404
    # authed but malformed site_id: uniform 404 before any DB work
    r = client.get("/admin/sites/not-a-uuid/competitors", headers={"X-Admin-Token": "sekret"})
    assert r.status_code == 404


@requires_db
def test_admin_competitors_empty_state_body(conn, site, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    r = client.get(
        f"/admin/sites/{site['site_id']}/competitors", headers={"X-Admin-Token": "sekret"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["you"]["domain"] == site["domain"]
    assert body["you"]["tracked_queries"] == 0
    assert body["you"]["rank_top3"] is None  # honest absence, not a fake zero
    assert body["competitors"] == []
    assert body["note"] == "no tracked queries yet; no competitors configured"
    assert body["feature_share"] == {"weeks": [], "queries": 0, "note": "no tracked queries yet"}
    # current-month window
    month_start = dt.date.today().replace(day=1)
    assert body["window"]["since"] == month_start.isoformat()

    # unknown-but-well-formed site id: uniform 404
    r = client.get(
        f"/admin/sites/{uuid.uuid4()}/competitors", headers={"X-Admin-Token": "sekret"}
    )
    assert r.status_code == 404
