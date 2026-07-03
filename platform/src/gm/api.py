"""FastAPI surface (docs/phase-b-wave2-contracts.md): public share reports + admin.

Two surfaces, deliberately asymmetric:

- /r/{token}: the ONLY unauthenticated tenant-data path. resolve_share runs
  without org context (token_hash lookup only); on a hit the org context is
  set from the resolved org_id before any other table is touched. Miss,
  expired, and revoked are all the same uniform 404 (no oracle). Responses
  carry a strict CSP (the report is inline-CSS-only by construction),
  no-referrer, and noindex. Guarded by a simple in-process token bucket —
  a STUB for real (per-IP / distributed) rate limiting later.

- /admin/*: header-guarded (X-Admin-Token == env ADMIN_TOKEN, compared
  constant-time); 404 when the env var is unset or the header is wrong, so
  the surface is indistinguishable from absent. Admin queries run without
  org context: in dev the app connects as the table owner (RLS bypass per
  001_phase_a.sql note); Supabase FORCE RLS hardening is Phase C.

DB access: a fresh gm.db.connect() per request — no pooling yet (solo scale).
"""

from __future__ import annotations

import os
import secrets
import threading
import time
import uuid
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from gm import db
from gm.delivery import report, shares

SHARE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
}

# Uniform for miss / expired / revoked / dangling share — no oracle detail.
_NOT_FOUND_HTML = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="robots" content="noindex"><title>Not found</title></head>'
    "<body><h1>404</h1><p>This report does not exist or is no longer available.</p>"
    "</body></html>"
)


class TokenBucket:
    """In-process global token bucket. Stub: real limiting (per-IP, shared
    across processes) comes later; this only blunts accidental hammering."""

    def __init__(self, capacity: float = 30.0, refill_per_minute: float = 30.0):
        self.capacity = float(capacity)
        self._refill_per_second = float(refill_per_minute) / 60.0
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self.capacity, self._tokens + (now - self._last) * self._refill_per_second
            )
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


_share_bucket = TokenBucket(capacity=30.0, refill_per_minute=30.0)


def _connect() -> psycopg.Connection:
    """One fresh connection per request (monkeypatch point for tests)."""
    return db.connect()


app = FastAPI(title="growthmonk", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


# ---------------------------------------------------------------------------
# Public share page
# ---------------------------------------------------------------------------

def _share_404() -> HTMLResponse:
    return HTMLResponse(_NOT_FOUND_HTML, status_code=404, headers=SHARE_HEADERS)


@app.get("/r/{token}", response_class=HTMLResponse)
def share_report(token: str):
    if not _share_bucket.allow():
        return JSONResponse({"detail": "rate limited"}, status_code=429)
    with _connect() as conn:
        share = shares.resolve_share(conn, token)
        conn.rollback()  # end the org-less transaction before scoping
        if share is None:
            return _share_404()
        db.set_org(conn, share["org_id"])  # implicit BEGIN + SET LOCAL app.org_id
        audit = conn.execute(
            "select * from audits where id = %s", (share["audit_id"],)
        ).fetchone()
        if audit is None:
            conn.rollback()
            return _share_404()
        findings = conn.execute(
            "select * from audit_findings where audit_id = %s order by check_id",
            (share["audit_id"],),
        ).fetchall()
        site = (
            conn.execute("select * from sites where id = %s", (audit["site_id"],)).fetchone()
            or {}
        )
        conn.rollback()  # read-only work; release the transaction
    return HTMLResponse(report.render_audit_html(audit, findings, site), headers=SHARE_HEADERS)


# ---------------------------------------------------------------------------
# Admin surface
# ---------------------------------------------------------------------------

def _require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    """404 (not 401/403) when ADMIN_TOKEN is unset or the header mismatches:
    the admin surface should be indistinguishable from a missing route."""
    expected = os.environ.get("ADMIN_TOKEN")
    if not expected or not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=404)


_admin = [Depends(_require_admin)]


@app.get("/admin/sites", dependencies=_admin)
def admin_sites():
    with _connect() as conn:
        rows = conn.execute(
            "select s.id, s.domain_norm as domain, s.is_control, o.name as org"
            " from sites s join orgs o on o.id = s.org_id"
            " order by o.name, s.domain_norm"
        ).fetchall()
        conn.rollback()
    return rows


@app.get("/admin/sites/{site_id}/timeline", dependencies=_admin)
def admin_site_timeline(site_id: str):
    try:
        sid = uuid.UUID(site_id)
    except ValueError as exc:
        raise HTTPException(status_code=404) from exc
    with _connect() as conn:
        job_rows = conn.execute(
            "select id, type, status, attempts, max_attempts, last_error,"
            " run_after, created_at, finished_at"
            " from jobs where site_id = %s order by created_at desc limit 50",
            (sid,),
        ).fetchall()
        run_rows = conn.execute(
            "select id, status, scheduled_for, started_at, finished_at"
            " from citation_runs where site_id = %s order by created_at desc limit 50",
            (sid,),
        ).fetchall()
        audit_rows = conn.execute(
            "select id, url, status, gate_state, scores->>'overall_grade' as grade,"
            " cost_cents, created_at, finished_at"
            " from audits where site_id = %s order by created_at desc limit 50",
            (sid,),
        ).fetchall()
        conn.rollback()
    return {"jobs": job_rows, "runs": run_rows, "audits": audit_rows}


@app.get("/admin/jobs/dead", dependencies=_admin)
def admin_dead_jobs():
    with _connect() as conn:
        rows = conn.execute(
            "select id, type, org_id, site_id, payload, attempts, max_attempts,"
            " last_error, created_at, finished_at"
            " from jobs where status = 'dead'"
            " order by coalesce(finished_at, created_at) desc limit 100"
        ).fetchall()
        conn.rollback()
    return rows


@app.post("/admin/jobs/{job_id}/retry", dependencies=_admin)
def admin_retry_job(job_id: int):
    with _connect() as conn:
        row = conn.execute(
            "update jobs set status = 'queued', attempts = 0, run_after = now(),"
            " locked_by = null, locked_until = null, finished_at = null"
            " where id = %s and status = 'dead' returning id",
            (job_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise HTTPException(status_code=404)
        conn.commit()
    return {"ok": True, "id": row["id"]}


@app.get("/admin/costs", dependencies=_admin)
def admin_costs():
    with _connect() as conn:
        rows = conn.execute(
            "select c.org_id, max(o.name) as org, sum(c.cost_cents) as cost_cents_30d,"
            " count(*) as events"
            " from cost_events c left join orgs o on o.id = c.org_id"
            " where c.created_at > now() - interval '30 days'"
            " group by c.org_id order by cost_cents_30d desc"
        ).fetchall()
        conn.rollback()
    return rows
