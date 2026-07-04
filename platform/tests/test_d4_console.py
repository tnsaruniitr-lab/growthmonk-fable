"""Tests for gm.delivery.console (Phase D4 WP-H).

Auth-guard matrix and /admin/ui smoke are pure (no network, no DB) and always
run — the guard fires before any handler touches a connection. The pure
`*_data` helpers run against a real migrated Postgres under the DATABASE_URL
skip guard: honest empty states on a fresh DB, then planted-data assertions
with an injected `now` (weeks Mon-start, months calendar).

The router is mounted on a local FastAPI app: api.py inclusion is WP-WIRE's.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gm.delivery import console

needs_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(console.router)
    return app


client = TestClient(_app())

JSON_ENDPOINTS = [
    "/admin/overview",
    "/admin/sites/overview",
    "/admin/jobs/recent",
    "/admin/queue",
    "/admin/citations/summary",
]

# Wednesday 2026-07-15; the Mon-start week is Jul 13..19, the month is July.
NOW = dt.datetime(2026, 7, 15, 12, 0, tzinfo=dt.UTC)


# ---------------------------------------------------------------------------
# Always-run: require_admin guard matrix on EVERY new endpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", JSON_ENDPOINTS)
def test_json_endpoints_404_when_env_unset(monkeypatch, path):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    assert client.get(path, headers={"X-Admin-Token": "anything"}).status_code == 404


@pytest.mark.parametrize("path", JSON_ENDPOINTS)
def test_json_endpoints_404_on_missing_or_wrong_header(monkeypatch, path):
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    assert client.get(path).status_code == 404
    assert client.get(path, headers={"X-Admin-Token": "wrong"}).status_code == 404


def test_ui_404_when_env_unset(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    assert client.get("/admin/ui").status_code == 404


def test_ui_smoke(monkeypatch):
    """200 with the env set (no header needed for navigation), all six
    anchors, zero external URLs, hardened headers, token plumbing present."""
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    r = client.get("/admin/ui")
    assert r.status_code == 200
    html = r.text
    for anchor in ("overview", "sites", "jobs", "queue", "citations", "spend"):
        assert f'id="{anchor}"' in html
        assert f'href="#{anchor}"' in html
    # zero external assets: no src/href pointing at http(s) or protocol-relative
    assert re.findall(r"""(?:src|href)\s*=\s*["']\s*(?:https?:|//)""", html) == []
    assert "gm_admin_token" in html  # the localStorage key
    assert "X-Admin-Token" in html  # fetches carry the header
    assert "/admin/spend" in html  # consumes WP-WIRE's endpoint
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'unsafe-inline'" in csp
    assert "connect-src 'self'" in csp
    assert r.headers["x-robots-tag"] == "noindex"
    assert r.headers["referrer-policy"] == "no-referrer"


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    from gm import db

    db.run_migrations()


@pytest.fixture()
def conn(_migrated):
    from gm import db

    with db.connect(autocommit=True) as c:
        c.execute(
            "truncate orgs, sites, pages, audits, audit_findings, serp_comparisons, briefs,"
            " content_items, drafts, publish_events, verify_events, content_deltas,"
            " site_deltas, levers, queue_items, tracked_prompts, citation_runs,"
            " citation_results, tracked_queries, rank_history, booked_leads, connections,"
            " cost_events, jobs, schedules restart identity cascade"
        )
        c.execute("truncate gsc_daily, gsc_ingest_log, gsc_page_daily, gsc_window_agg")
        yield c


def _org(conn) -> str:
    return conn.execute("insert into orgs (name) values ('t') returning id").fetchone()["id"]


def _site(conn, org, domain="ex.com", *, control=False) -> str:
    return conn.execute(
        "insert into sites (org_id, domain_norm, is_control) values (%s, %s, %s) returning id",
        (org, domain, control),
    ).fetchone()["id"]


def _ts(y, m, d, hh=6, mm=0) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=dt.UTC)


def _audit(conn, org, site, *, created, status="done", gate="ok", score=None, draft=False):
    from psycopg.types.json import Jsonb

    scores = {} if score is None else {"overall_score": score, "overall_grade": "B+"}
    conn.execute(
        "insert into audits (org_id, site_id, registry_version, status, gate_state,"
        " scores, draft_id, created_at, finished_at)"
        " values (%s, %s, 'r1', %s, %s, %s, %s, %s, %s)",
        (org, site, status, gate, Jsonb(scores),
         uuid.uuid4() if draft else None, created, created),
    )


def _run(conn, org, site, *, created, scheduled=None, status="done"):
    from psycopg.types.json import Jsonb

    return conn.execute(
        "insert into citation_runs (org_id, site_id, panel, status, scheduled_for, created_at)"
        " values (%s, %s, %s, %s, %s, %s) returning id",
        (org, site, Jsonb([]), status, scheduled or created, created),
    ).fetchone()["id"]


def _prompt(conn, org, site, text, *, active=True) -> str:
    return conn.execute(
        "insert into tracked_prompts (org_id, site_id, prompt, prompt_hash, engines, active)"
        " values (%s, %s, %s, md5(%s), '{openai}', %s) returning id",
        (org, site, text, text, active),
    ).fetchone()["id"]


def _sample(conn, org, run, prompt, idx, *, cited=False, mentioned=False, error=None,
            sampled=None):
    conn.execute(
        "insert into citation_results (org_id, run_id, prompt_id, engine, sample_index,"
        " cited, mentioned, error, sampled_at)"
        " values (%s, %s, %s, 'openai', %s, %s, %s, %s, %s)",
        (org, run, prompt, idx, cited, mentioned, error, sampled or NOW),
    )


# ---------------------------------------------------------------------------
# overview_data: shape + honest empty states on a fresh migrated DB
# ---------------------------------------------------------------------------


@needs_db
def test_overview_data_empty_shape_and_honesty(conn):
    data = console.overview_data(conn, now=NOW)

    assert data["sites"] == {"total": 0, "control": 0}
    assert [s["id"] for s in data["stages"]] == [
        "audit", "compare", "brief", "draft", "publish", "verify",
        "measure", "receipt", "prove",
    ]
    for stage in data["stages"]:
        assert set(stage) == {"id", "label", "caption", "counts", "note"}
        assert stage["label"] and stage["caption"]
        # fresh DB: every stage has an honest human-sentence note
        assert isinstance(stage["note"], str) and stage["note"]

    counts = {s["id"]: s["counts"] for s in data["stages"]}
    # true zeros are zeros; zero-denominator aggregates are None — never 0
    assert counts["audit"] == {"audits_this_month": 0, "median_score": None}
    assert counts["compare"] == {"comparative_audits_this_month": 0}
    assert counts["brief"] == {"briefs_this_month": 0}
    assert counts["draft"] == {"drafts_in_flight": 0}
    assert counts["publish"] == {"publish_events_this_month": 0}
    assert counts["verify"] == {"verify_events_this_month": 0}
    assert counts["measure"] == {
        "tracked_queries": 0, "booked_leads_this_week": 0, "latest_gsc_final": None,
    }
    assert counts["receipt"] == {"receipts_assembled": 0, "latest_period": None}
    assert counts["prove"] == {
        "prompts_tracked": 0, "runs_this_week": 0, "samples_ok": 0, "samples_err": 0,
    }
    assert data["queue_open_by_kind"] == {}
    assert data["next_jobs"] == []


@needs_db
def test_overview_data_planted(conn):
    from psycopg.types.json import Jsonb

    org = _org(conn)
    site = _site(conn, org, "ex.com")
    ctrl = _site(conn, org, "ctrl.com", control=True)

    # audit: two countable this month (median 70.0); a draft scorecard, a
    # competitor reference, a failed run and a last-month audit all excluded
    _audit(conn, org, site, created=_ts(2026, 7, 10), score=80.0)
    _audit(conn, org, site, created=_ts(2026, 7, 2), score=60.0)
    _audit(conn, org, site, created=_ts(2026, 7, 11), score=99.0, draft=True)
    _audit(conn, org, site, created=_ts(2026, 7, 12), score=99.0, gate="competitor_reference")
    _audit(conn, org, site, created=_ts(2026, 7, 13), status="failed")
    _audit(conn, org, site, created=_ts(2026, 6, 20), score=10.0)

    conn.execute(
        "insert into serp_comparisons (org_id, site_id, query_norm, created_at)"
        " values (%s, %s, 'q', %s)",
        (org, site, _ts(2026, 7, 5)),
    )
    conn.execute(
        "insert into briefs (org_id, site_id, target, created_at) values (%s, %s, %s, %s)",
        (org, site, Jsonb({"query": "q", "kind": "new"}), _ts(2026, 7, 3)),
    )
    for status in ("drafting", "review", "published"):
        conn.execute(
            "insert into content_items (org_id, site_id, kind, status)"
            " values (%s, %s, 'new', %s)",
            (org, site, status),
        )
    ci = conn.execute("select id from content_items limit 1").fetchone()["id"]
    conn.execute(
        "insert into publish_events (org_id, content_item_id, target, published_at)"
        " values (%s, %s, 'wordpress', %s)",
        (org, ci, _ts(2026, 7, 6)),
    )
    conn.execute(
        "insert into verify_events (org_id, content_item_id, kind, at)"
        " values (%s, %s, 'bev', %s)",
        (org, ci, _ts(2026, 7, 6)),
    )

    conn.execute(
        "insert into tracked_queries (org_id, site_id, query_norm, active) values"
        " (%s, %s, 'a', true), (%s, %s, 'b', true), (%s, %s, 'c', false)",
        (org, site, org, site, org, site),
    )
    # Mon-start week boundary: Mon Jul 13 00:30 is in; Sun Jul 12 23:00 is out
    conn.execute(
        "insert into booked_leads (org_id, site_id, source, occurred_at) values"
        " (%s, %s, 'manual', %s), (%s, %s, 'manual', %s)",
        (org, site, _ts(2026, 7, 13, 0, 30), org, site, _ts(2026, 7, 12, 23, 0)),
    )
    conn.execute(
        "insert into gsc_ingest_log (site_id, date, rows, final) values"
        " (%s, '2026-07-10', 5, true), (%s, '2026-07-12', 5, false)",
        (site, site),
    )
    conn.execute(
        "insert into site_deltas (org_id, site_id, period) values"
        " (%s, %s, '2026-05'), (%s, %s, '2026-06')",
        (org, site, org, site),
    )

    p1 = _prompt(conn, org, site, "best clinic")
    _prompt(conn, org, site, "top clinic")
    _prompt(conn, org, site, "old prompt", active=False)
    r1 = _run(conn, org, site, created=_ts(2026, 7, 14))  # this week
    r2 = _run(conn, org, site, created=_ts(2026, 7, 8))  # last week
    _sample(conn, org, r1, p1, 0, cited=True, sampled=_ts(2026, 7, 14))
    _sample(conn, org, r1, p1, 1, sampled=_ts(2026, 7, 14))
    _sample(conn, org, r1, p1, 2, error="boom", sampled=_ts(2026, 7, 14))
    _sample(conn, org, r2, p1, 0, cited=True, sampled=_ts(2026, 7, 8))  # out of week

    for i, kind in enumerate(["striking_distance", "striking_distance", "keyword_gap"]):
        conn.execute(
            "insert into queue_items (org_id, site_id, kind, target, target_hash, at_stake)"
            " values (%s, %s, %s, %s, %s, %s)",
            (org, site, kind, Jsonb({"q": i}), f"h{i}", Jsonb({})),
        )
    conn.execute(
        "insert into queue_items (org_id, site_id, kind, target, target_hash, at_stake,"
        " status) values (%s, %s, 'decay', %s, 'h9', %s, 'dismissed')",
        (org, site, Jsonb({}), Jsonb({})),
    )
    conn.execute(
        "insert into schedules (org_id, site_id, job_type, every_minutes, next_run_at,"
        " enabled) values (%s, %s, 'track_serps', 10080, %s, true),"
        " (%s, %s, 'keyword_gap', 43200, %s, false)",
        (org, site, NOW + dt.timedelta(minutes=60), org, ctrl, NOW),
    )

    data = console.overview_data(conn, now=NOW)
    assert data["sites"] == {"total": 2, "control": 1}
    counts = {s["id"]: s["counts"] for s in data["stages"]}
    notes = {s["id"]: s["note"] for s in data["stages"]}
    assert counts["audit"] == {"audits_this_month": 2, "median_score": 70.0}
    assert notes["audit"] is None
    assert counts["compare"] == {"comparative_audits_this_month": 1}
    assert counts["brief"] == {"briefs_this_month": 1}
    assert counts["draft"] == {"drafts_in_flight": 2}
    assert counts["publish"] == {"publish_events_this_month": 1}
    assert counts["verify"] == {"verify_events_this_month": 1}
    assert counts["measure"] == {
        "tracked_queries": 2,
        "booked_leads_this_week": 1,
        "latest_gsc_final": dt.date(2026, 7, 10),
    }
    assert notes["measure"] is None
    assert counts["receipt"] == {"receipts_assembled": 2, "latest_period": "2026-06"}
    assert counts["prove"] == {
        "prompts_tracked": 2, "runs_this_week": 1, "samples_ok": 2, "samples_err": 1,
    }
    assert notes["prove"] is None
    assert data["queue_open_by_kind"] == {"keyword_gap": 1, "striking_distance": 2}
    # the disabled schedule is excluded; eta from the injected now
    assert data["next_jobs"] == [
        {
            "job_type": "track_serps",
            "site": "ex.com",
            "next_run_at": NOW + dt.timedelta(minutes=60),
            "eta_minutes": 60,
        }
    ]


# ---------------------------------------------------------------------------
# sites / jobs / queue helpers
# ---------------------------------------------------------------------------


@needs_db
def test_sites_overview_data(conn):
    org = _org(conn)
    site = _site(conn, org, "ex.com")
    ctrl = _site(conn, org, "ctrl.com", control=True)
    conn.execute(
        "update sites set competitor_domains = '{a.com,b.com}' where id = %s", (site,)
    )
    conn.execute(
        "insert into tracked_queries (org_id, site_id, query_norm, active) values"
        " (%s, %s, 'a', true), (%s, %s, 'b', false)",
        (org, site, org, site),
    )
    _prompt(conn, org, site, "best clinic")
    _audit(conn, org, site, created=_ts(2026, 7, 10), score=80.0)
    conn.execute(
        "insert into schedules (org_id, site_id, job_type, every_minutes, next_run_at,"
        " enabled) values (%s, %s, 'track_serps', 10080, %s, true)",
        (org, site, NOW),
    )

    rows = console.sites_overview_data(conn)
    assert [r["domain"] for r in rows] == ["ctrl.com", "ex.com"]
    by_domain = {r["domain"]: r for r in rows}

    ex = by_domain["ex.com"]
    assert ex["org"] == "t" and ex["is_control"] is False
    assert ex["tracked_queries"] == 1 and ex["tracked_prompts"] == 1
    assert ex["competitors"] == 2
    assert ex["last_audit"]["grade"] == "B+"
    assert ex["last_audit"]["at"] is not None
    assert ex["schedules"] == [
        {"job_type": "track_serps", "every_minutes": 10080, "next_run_at": NOW,
         "enabled": True}
    ]

    cc = by_domain["ctrl.com"]
    assert cc["is_control"] is True
    assert cc["last_audit"] is None  # never audited — None, not a fake row
    assert cc["schedules"] == []
    assert cc["tracked_queries"] == 0 and cc["competitors"] == 0
    assert str(site) == ex["site_id"] and str(ctrl) == cc["site_id"]


@needs_db
def test_jobs_recent_data_join_order_and_limit(conn):
    org = _org(conn)
    site = _site(conn, org, "ex.com")
    for i, status in enumerate(["done", "failed", "dead"]):
        conn.execute(
            "insert into jobs (type, org_id, site_id, status, created_at, last_error)"
            " values ('t', %s, %s, %s, %s, %s)",
            (org, site, status, _ts(2026, 7, 1 + i), "err" if status != "done" else None),
        )
    rows = console.jobs_recent_data(conn, limit=2)
    assert [r["status"] for r in rows] == ["dead", "failed"]  # newest first, any status
    assert rows[0]["site"] == "ex.com"
    assert rows[0]["last_error"] == "err"
    assert len(console.jobs_recent_data(conn, limit=50)) == 3
    assert len(console.jobs_recent_data(conn, limit=-5)) == 1  # clamped


@needs_db
def test_queue_data_normalizer_absent_is_honest(conn, monkeypatch):
    from psycopg.types.json import Jsonb

    org = _org(conn)
    site = _site(conn, org, "ex.com")
    conn.execute(
        "insert into queue_items (org_id, site_id, kind, target, target_hash, at_stake)"
        " values (%s, %s, 'striking_distance', %s, 'h1', %s)",
        (org, site, Jsonb({"query": "q"}), Jsonb({"est_clicks_gain": 12})),
    )
    monkeypatch.setattr(console, "_normalize_at_stake_fn", lambda: None)
    data = console.queue_data(conn)
    assert data["normalizer_available"] is False
    assert data["note"] == "display normalizer not deployed"
    assert data["summary"] == [{"kind": "striking_distance", "status": "open", "n": 1}]
    (item,) = data["items"]
    assert item["display"] is None
    assert item["at_stake"] == {"est_clicks_gain": 12}  # raw payload, not invented
    assert item["site"] == "ex.com"


@needs_db
def test_queue_data_normalizer_present(conn, monkeypatch):
    from psycopg.types.json import Jsonb

    org = _org(conn)
    site = _site(conn, org, "ex.com")
    conn.execute(
        "insert into queue_items (org_id, site_id, kind, target, target_hash, at_stake)"
        " values (%s, %s, 'keyword_gap', %s, 'h1', %s)",
        (org, site, Jsonb({"query": "q"}), Jsonb({"volume": 900})),
    )

    def fake_normalize(item):
        assert item["kind"] == "keyword_gap"
        assert item["at_stake"] == {"volume": 900}
        return {"kind": item["kind"], "headline": "900 searches/mo",
                "detail": "best: a.com at #3", "value": 900.0, "unit": "searches/mo"}

    monkeypatch.setattr(console, "_normalize_at_stake_fn", lambda: fake_normalize)
    data = console.queue_data(conn)
    assert data["normalizer_available"] is True
    assert data["note"] is None
    assert data["items"][0]["display"]["headline"] == "900 searches/mo"


# ---------------------------------------------------------------------------
# citations summary: rate honesty + Gate-1 split
# ---------------------------------------------------------------------------


@needs_db
def test_citations_summary_rates_and_gate1(conn):
    org = _org(conn)
    treat = _site(conn, org, "ex.com")
    bare = _site(conn, org, "new.com")
    _site(conn, org, "ctrl.com", control=True)  # control: excluded from gate1

    conn.execute(
        "insert into levers (org_id, site_id, applied_at, lever_class, description)"
        " values (%s, %s, '2026-07-10', 'onsite_fix', 'd'),"
        " (%s, %s, '2026-07-20', 'schema', 'later lever ignored for the split')",
        (org, treat, org, treat),
    )
    # done runs: Jul 8 -> baseline; Jul 10 & Jul 14 -> treatment (>= first lever)
    r_base = _run(conn, org, treat, created=_ts(2026, 7, 8))
    _run(conn, org, treat, created=_ts(2026, 7, 10))
    _run(conn, org, treat, created=_ts(2026, 7, 14))
    _run(conn, org, treat, created=_ts(2026, 7, 15), status="failed")  # not counted
    _run(conn, org, bare, created=_ts(2026, 7, 9))  # no lever -> baseline

    p1 = _prompt(conn, org, treat, "best clinic")
    p2 = _prompt(conn, org, treat, "top clinic")
    # p1: 2 clean samples (1 cited, 2 mentioned) + 1 errored (excluded BOTH sides)
    _sample(conn, org, r_base, p1, 0, cited=True, mentioned=True)
    _sample(conn, org, r_base, p1, 1, mentioned=True)
    _sample(conn, org, r_base, p1, 2, cited=True, error="boom")
    # p2: only an errored sample -> rates None, never 0
    _sample(conn, org, r_base, p2, 0, cited=True, error="boom")

    data = console.citations_summary_data(conn, now=NOW)

    assert data["gate1"]["verdict_date"] == "2026-09-01"
    assert data["gate1"]["days_to_verdict"] == 48
    sites = {s["site"]: s for s in data["gate1"]["sites"]}
    assert set(sites) == {"ex.com", "new.com"}  # control excluded
    ex = sites["ex.com"]
    assert ex["first_lever_at"] == dt.date(2026, 7, 10)
    assert ex["baseline_done"] == 1 and ex["treatment_done"] == 2
    assert ex["baseline_target"] == 3 and ex["treatment_target"] == 3
    assert ex["note"] is None
    nw = sites["new.com"]
    assert nw["first_lever_at"] is None
    assert nw["baseline_done"] == 1
    assert nw["treatment_done"] is None  # no lever -> no treatment period, not 0
    assert nw["note"] == "no lever logged yet"

    assert len(data["recent_runs"]) == 5
    assert data["recent_runs"][0]["status"] == "failed"  # newest first
    by_run = {r["id"]: r for r in data["recent_runs"]}
    assert by_run[str(r_base)]["samples_ok"] == 2
    assert by_run[str(r_base)]["samples_err"] == 2

    rates = {r["prompt"]: r for r in data["prompt_rates"]}
    assert rates["best clinic"]["samples"] == 2
    assert rates["best clinic"]["cited_rate"] == 0.5
    assert rates["best clinic"]["mentioned_rate"] == 1.0
    assert rates["top clinic"]["samples"] == 0
    assert rates["top clinic"]["cited_rate"] is None
    assert rates["top clinic"]["mentioned_rate"] is None


# ---------------------------------------------------------------------------
# endpoints serve the pure helpers' shapes (auth passed)
# ---------------------------------------------------------------------------


@needs_db
def test_endpoints_serve_data_with_token(conn, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    headers = {"X-Admin-Token": "sekret"}

    r = client.get("/admin/overview", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"sites", "stages", "queue_open_by_kind", "next_jobs"}
    assert len(body["stages"]) == 9

    r = client.get("/admin/sites/overview", headers=headers)
    assert r.status_code == 200 and r.json() == []

    r = client.get("/admin/jobs/recent?limit=5", headers=headers)
    assert r.status_code == 200 and r.json() == []

    r = client.get("/admin/queue", headers=headers)
    assert r.status_code == 200
    assert set(r.json()) == {"summary", "items", "normalizer_available", "note"}

    r = client.get("/admin/citations/summary", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"recent_runs", "prompt_rates", "gate1"}
    assert body["gate1"]["verdict_date"] == "2026-09-01"
