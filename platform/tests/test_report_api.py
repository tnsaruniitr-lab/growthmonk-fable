"""Tests for gm.delivery.shares / gm.delivery.report / gm.api.

Renderer and header/guard tests are pure and always run (no network, no DB —
the /r miss path is exercised with a fake connection). Share create/resolve/
expiry/revoked and the end-to-end share page run only under DATABASE_URL.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from gm import api, db
from gm.delivery import report, shares

needs_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")

client = TestClient(api.app)

EVIL = '<img src=x onerror=alert(1)>'

_SCORES = {
    "overall_grade": "B+",
    "overall_score": 80.9,
    "page_citation_readiness": 80.9,
    "demand_capture": 81.3,
    "gate_state": "ok",
    "section_scores": {"A_technical": 90.0, "D_schema": 55.5},
    "section_counts": {
        "A_technical": {"pass": 9, "warn": 1, "fail": 0, "na": 0, "inconclusive": 0}
    },
}


def _audit(**over) -> dict:
    base = {
        "url": "https://example.com/page",
        "registry_version": "2026.07-r1",
        "model_version": "claude-sonnet-5",
        "gate_state": "ok",
        "scores": dict(_SCORES),
        "finished_at": None,
        "created_at": None,
    }
    base.update(over)
    return base


def _finding(**over) -> dict:
    base = {
        "check_id": "A-01",
        "check_version": 1,
        "status": "fail",
        "badge": "hard_evidence",
        "fix_type": "page_html",
        "evidence": {"note": "title tag missing"},
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Always-run: healthz + admin guard
# ---------------------------------------------------------------------------


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_admin_404_when_env_unset(monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    r = client.get("/admin/sites", headers={"X-Admin-Token": "anything"})
    assert r.status_code == 404


def test_admin_404_on_wrong_or_missing_token(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    assert client.get("/admin/sites", headers={"X-Admin-Token": "nope"}).status_code == 404
    assert client.get("/admin/sites").status_code == 404
    assert client.post("/admin/jobs/1/retry", headers={"X-Admin-Token": "nope"}).status_code == 404


# ---------------------------------------------------------------------------
# Always-run: renderer (pure) — XSS escaping is the contract-critical test
# ---------------------------------------------------------------------------


def test_render_escapes_evidence_note():
    html_out = report.render_audit_html(
        _audit(), [_finding(evidence={"note": EVIL})], {"domain_norm": "example.com"}
    )
    assert EVIL not in html_out
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_out
    assert "<script" not in html_out.lower()


def test_render_escapes_site_and_forged_scores():
    site = {"domain_norm": '"><script>alert(2)</script>.com'}
    audit = _audit(scores={**_SCORES, "overall_grade": "<b>A++</b>"})
    html_out = report.render_audit_html(audit, [], site)
    assert "<script" not in html_out.lower()
    assert "<b>" not in html_out
    assert "&lt;b&gt;A++&lt;/b&gt;" in html_out


def test_render_structure_and_grouping():
    findings = [
        _finding(check_id="D-02", status="warn", badge="static_rule", fix_type="schema",
                 evidence={"note": "no FAQPage schema"}),
        _finding(check_id="A-01", status="fail"),
        _finding(check_id="A-03", status="pass", evidence={"note": "ok"}),
    ]
    html_out = report.render_audit_html(_audit(), findings, {"domain_norm": "example.com"})
    assert html_out.startswith("<!doctype html>")
    assert "@media print" in html_out
    assert "<script" not in html_out.lower()
    # header numbers
    assert "example.com" in html_out
    # display scores are rounded for the client artifact (81.3 -> 81)
    assert "B+" in html_out and ">81<" in html_out
    # fix_type group headings, page_html before schema
    assert html_out.index("Page HTML fixes (2)") < html_out.index("Schema markup fixes (1)")
    # badge chips + failures sort before passes inside a group
    assert "hard evidence" in html_out and "static rule" in html_out
    assert html_out.index("A-01") < html_out.index("A-03")


def test_render_tolerates_missing_scores_and_findings():
    html_out = report.render_audit_html(
        {"scores": None, "gate_state": "transport_inconclusive"}, [], {}
    )
    assert "INCONCLUSIVE" in html_out
    assert "No findings recorded" in html_out


# ---------------------------------------------------------------------------
# Always-run: /r/{token} miss path via a fake connection (headers + uniform 404)
# ---------------------------------------------------------------------------


class _FakeCursor:
    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakeConn:
    def execute(self, *args, **kwargs):
        return _FakeCursor()

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_share_miss_uniform_404_and_headers(monkeypatch):
    monkeypatch.setattr(api, "_connect", lambda: _FakeConn())
    r = client.get("/r/not-a-real-token")
    assert r.status_code == 404
    assert r.headers["content-security-policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'"
    )
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-robots-tag"] == "noindex"
    assert "no longer available" in r.text  # generic body, no oracle detail


def test_share_rate_limit_429(monkeypatch):
    monkeypatch.setattr(api, "_connect", lambda: _FakeConn())
    monkeypatch.setattr(api, "_share_bucket", api.TokenBucket(capacity=2, refill_per_minute=0))
    assert client.get("/r/x").status_code == 404
    assert client.get("/r/x").status_code == 404
    assert client.get("/r/x").status_code == 429


def test_resolve_share_rejects_absurd_tokens():
    # length guard fires before any SQL — a fake conn that would explode is safe
    assert shares.resolve_share(None, "") is None
    assert shares.resolve_share(None, "x" * 4096) is None


# ---------------------------------------------------------------------------
# DB-backed: shares lifecycle + end-to-end share page + admin routes
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    db.run_migrations()


@pytest.fixture()
def seeded(_migrated):
    from psycopg.types.json import Jsonb

    tag = uuid.uuid4().hex[:10]
    with db.connect(autocommit=True) as conn:
        org_id = conn.execute(
            "insert into orgs (name) values (%s) returning id", (f"t-{tag}",)
        ).fetchone()["id"]
        site_id = conn.execute(
            "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
            (org_id, f"{tag}.example.com"),
        ).fetchone()["id"]
        audit_id = conn.execute(
            "insert into audits (org_id, site_id, url, registry_version, model_version,"
            " status, gate_state, scores) values (%s, %s, %s, %s, %s, 'done', 'ok', %s)"
            " returning id",
            (org_id, site_id, f"https://{tag}.example.com/", "2026.07-r1",
             "claude-sonnet-5", Jsonb(_SCORES)),
        ).fetchone()["id"]
        conn.execute(
            "insert into audit_findings (org_id, audit_id, check_id, check_version,"
            " status, badge, fix_type, evidence) values (%s, %s, 'A-01', 1, 'fail',"
            " 'hard_evidence', 'page_html', %s)",
            (org_id, audit_id, Jsonb({"note": EVIL})),
        )
    return {
        "org": str(org_id),
        "site": str(site_id),
        "audit": str(audit_id),
        "domain": f"{tag}.example.com",
    }


@needs_db
def test_share_create_and_resolve(seeded):
    with db.connect() as conn:
        token = shares.create_share(conn, seeded["org"], seeded["audit"])
        conn.commit()
    assert len(token) >= 40
    with db.connect() as conn:
        got = shares.resolve_share(conn, token)
        assert got == {"audit_id": seeded["audit"], "org_id": seeded["org"]}
        assert shares.resolve_share(conn, token + "x") is None


@needs_db
def test_share_expiry_enforced(seeded):
    with db.connect() as conn:
        token = shares.create_share(conn, seeded["org"], seeded["audit"], ttl_days=1)
        conn.commit()
    with db.connect(autocommit=True) as conn:
        conn.execute(
            "update report_shares set expires_at = now() - interval '1 minute'"
            " where audit_id = %s",
            (seeded["audit"],),
        )
    with db.connect() as conn:
        assert shares.resolve_share(conn, token) is None


@needs_db
def test_share_revoked_enforced(seeded):
    with db.connect() as conn:
        token = shares.create_share(conn, seeded["org"], seeded["audit"])
        conn.commit()
    with db.connect(autocommit=True) as conn:
        conn.execute(
            "update report_shares set revoked = true where audit_id = %s", (seeded["audit"],)
        )
    with db.connect() as conn:
        assert shares.resolve_share(conn, token) is None


@needs_db
def test_share_page_end_to_end(seeded):
    with db.connect() as conn:
        token = shares.create_share(conn, seeded["org"], seeded["audit"])
        conn.commit()
    r = client.get(f"/r/{token}")
    assert r.status_code == 200
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert r.headers["x-robots-tag"] == "noindex"
    assert seeded["domain"] in r.text
    assert EVIL not in r.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in r.text
    # expired/revoked come back as the same uniform 404
    assert client.get("/r/" + "A" * 43).status_code == 404


@needs_db
def test_admin_sites_costs_timeline(seeded, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    hdr = {"X-Admin-Token": "sekret"}

    with db.connect(autocommit=True) as conn:
        conn.execute(
            "insert into cost_events (org_id, provider, purpose, cost_cents)"
            " values (%s, 'anthropic', 'audit_classify', 12.5)",
            (seeded["org"],),
        )

    sites_resp = client.get("/admin/sites", headers=hdr)
    assert sites_resp.status_code == 200
    assert any(s["domain"] == seeded["domain"] for s in sites_resp.json())

    costs_resp = client.get("/admin/costs", headers=hdr)
    assert costs_resp.status_code == 200
    mine = [c for c in costs_resp.json() if c["org_id"] == seeded["org"]]
    assert mine and float(mine[0]["cost_cents_30d"]) == pytest.approx(12.5)

    tl = client.get(f"/admin/sites/{seeded['site']}/timeline", headers=hdr)
    assert tl.status_code == 200
    body = tl.json()
    assert set(body) == {"jobs", "runs", "audits"}
    assert any(a["id"] == seeded["audit"] for a in body["audits"])
    assert client.get("/admin/sites/not-a-uuid/timeline", headers=hdr).status_code == 404


@needs_db
def test_admin_dead_job_listed_and_retried(seeded, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "sekret")
    hdr = {"X-Admin-Token": "sekret"}

    with db.connect(autocommit=True) as conn:
        job_id = conn.execute(
            "insert into jobs (type, org_id, site_id, status, attempts, max_attempts,"
            " last_error, finished_at) values ('audit_page', %s, %s, 'dead', 3, 3,"
            " 'boom', now()) returning id",
            (seeded["org"], seeded["site"]),
        ).fetchone()["id"]

    dead = client.get("/admin/jobs/dead", headers=hdr)
    assert dead.status_code == 200
    assert any(j["id"] == job_id for j in dead.json())

    retry = client.post(f"/admin/jobs/{job_id}/retry", headers=hdr)
    assert retry.status_code == 200
    assert retry.json() == {"ok": True, "id": job_id}

    with db.connect(autocommit=True) as conn:
        row = conn.execute("select * from jobs where id = %s", (job_id,)).fetchone()
    assert row["status"] == "queued"
    assert row["attempts"] == 0
    assert row["locked_by"] is None and row["locked_until"] is None
    assert row["finished_at"] is None

    # retrying a non-dead job is a 404
    assert client.post(f"/admin/jobs/{job_id}/retry", headers=hdr).status_code == 404
