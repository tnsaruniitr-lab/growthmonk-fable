"""Internal operator console (Phase D4 WP-H, docs/phase-d4-contracts.md).

Phase B deliverable 7's founder-only admin grown to Phase-D scope. NOT a
product surface (docs/01-product.md §6): token-gated behind ADMIN_TOKEN,
never linked from reports/receipts, no share tokens, no theming.

Design:

- `router` is a FastAPI APIRouter; api.py includes it in WP-WIRE. Every JSON
  endpoint carries `_require_admin` — a LOCAL COPY of api.py's underscore name
  (labs.py precedent for copying private helpers instead of importing them):
  X-Admin-Token vs env ADMIN_TOKEN, compared constant-time, 404 when unset or
  wrong so the surface is indistinguishable from absent.
- `GET /admin/ui` serves a single self-contained HTML shell WITHOUT the header
  (browsers cannot attach headers on navigation) but still 404s when
  ADMIN_TOKEN is unset. The shell holds ZERO tenant data: everything arrives
  via fetch with X-Admin-Token, prompted once and kept in localStorage
  (`gm_admin_token`). A 404 from a console data endpoint clears the stored
  token and re-prompts — a wrong token stays indistinguishable from an absent
  surface. The one sanctioned exception: `/admin/spend` belongs to WP-WIRE and
  may not be deployed yet, so its 404 renders "not wired yet" AFTER the token
  has already been proven against /admin/overview.
- Endpoints are thin wrappers over pure `*_data(conn)` helpers (testable
  without HTTP); read-only work ends with rollback (api.py's discipline).
- Empty-state law: count(*) zeros are honest zeros; medians/rates with a zero
  denominator are None, rendered "no data yet" — never 0, never invented.
- Weeks are Monday-start (D1 convention), months are calendar months, and
  `now` is injectable for deterministic tests.
"""

from __future__ import annotations

import datetime as dt
import os
import secrets
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse

from gm import db

router = APIRouter()

# Gate-1 exit gate (docs/03-roadmap.md): Sep 1 verdict, 3 baseline + 3 treatment
# panel runs per treatment site, split at the site's first lever.
GATE1_VERDICT_DATE = dt.date(2026, 9, 1)
GATE1_RUNS_TARGET = 3

CONSOLE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';"
        " connect-src 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
}

# audits rows whose gate_state marks them as something other than a client page
# audit: competitor references (compare.py) and group rollups (group.py) are
# excluded from stage counts and from "last audit" per site.
_NON_PAGE_GATE_STATES = ("competitor_reference", "group_rollup")

# The nine-stage engine loop, in order, with plain-English captions — the
# single copy; the UI renders captions straight from /admin/overview JSON.
STAGE_DEFS: tuple[tuple[str, str, str], ...] = (
    ("audit", "Audit",
     "A page is fetched and scored against 106 deterministic checks — "
     "same page in, same grade out."),
    ("compare", "Compare",
     "The page is lined up against competitors who actually rank for the "
     "query; the gaps they pass and we fail become the to-do list."),
    ("brief", "Brief",
     "Gaps turn into a written brief: what the page must cover to close them."),
    ("draft", "Draft",
     "A draft is written to the brief and re-scored before any human review."),
    ("publish", "Publish",
     "The approved draft goes live (WordPress or export) and the exact moment "
     "is recorded."),
    ("verify", "Verify",
     "The live page is re-fetched to confirm the published change really stuck."),
    ("measure", "Measure",
     "Search Console, tracked rankings and booked leads record what happened next."),
    ("receipt", "Receipt",
     "Once a month the evidence is assembled into a receipt: what changed and "
     "what moved."),
    ("prove", "Prove",
     "AI engines are re-asked the tracked prompts; every time the client is "
     "cited, it is logged."),
)


def _require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    """404 (not 401/403) when ADMIN_TOKEN is unset or the header mismatches:
    the admin surface should be indistinguishable from a missing route.
    Local copy of gm.api._require_admin (WP-H owns no line of api.py)."""
    expected = os.environ.get("ADMIN_TOKEN")
    if not expected or not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=404)


_admin = [Depends(_require_admin)]


def _connect() -> psycopg.Connection:
    """One fresh connection per request (monkeypatch point for tests)."""
    return db.connect()


# ---------------------------------------------------------------------------
# time helpers (weeks Mon-start, months calendar, now injectable)
# ---------------------------------------------------------------------------

def _norm_now(now: dt.datetime | None) -> dt.datetime:
    if now is None:
        return dt.datetime.now(dt.UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=dt.UTC)
    return now.astimezone(dt.UTC)


def _midnight(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC)


def _month_bounds(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """[first of this month, first of next month) as UTC datetimes."""
    start = now.date().replace(day=1)
    end = dt.date(start.year + 1, 1, 1) if start.month == 12 else \
        dt.date(start.year, start.month + 1, 1)
    return _midnight(start), _midnight(end)


def _week_bounds(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """[Monday of this week, next Monday) as UTC datetimes (D1 convention)."""
    monday = now.date() - dt.timedelta(days=now.date().weekday())
    return _midnight(monday), _midnight(monday + dt.timedelta(days=7))


def _scalar(conn: psycopg.Connection, sql: str, params: tuple = ()):
    row = conn.execute(sql, params).fetchone()
    return next(iter(row.values())) if row else None


# ---------------------------------------------------------------------------
# pure data helpers (no HTTP; every endpoint is a thin wrapper over one)
# ---------------------------------------------------------------------------

def overview_data(conn: psycopg.Connection, *, now: dt.datetime | None = None) -> dict:
    """The "what is this machine doing" view: nine ordered stages with live
    counts plus queue totals and the next scheduled jobs. Pure of HTTP.

    Honesty rules: stage counts are true zeros when the table says zero;
    median_score / latest_gsc_final / latest_period are None (never 0) when
    there is no data; each stage carries a human-sentence `note` when it has
    nothing to show, else note=None.
    """
    now = _norm_now(now)
    m0, m1 = _month_bounds(now)
    w0, w1 = _week_bounds(now)

    sites = conn.execute(
        "select count(*)::int as total, count(*) filter (where is_control)::int as control"
        " from sites"
    ).fetchone()

    audit_row = conn.execute(
        "select count(*)::int as n,"
        " percentile_cont(0.5) within group (order by (scores->>'overall_score')::float8)"
        "   filter (where scores->>'overall_score' is not null) as median_score"
        " from audits"
        " where status = 'done' and draft_id is null"
        " and coalesce(gate_state, '') not in ('competitor_reference', 'group_rollup')"
        " and created_at >= %s and created_at < %s",
        (m0, m1),
    ).fetchone()
    audits_n = audit_row["n"]
    median_score = audit_row["median_score"]
    if audits_n == 0:
        audit_note = "No page audits this month yet."
    elif median_score is None:
        audit_note = "Audits ran this month but none produced a numeric score."
    else:
        audit_note = None

    compare_n = _scalar(
        conn,
        "select count(*)::int from serp_comparisons where created_at >= %s and created_at < %s",
        (m0, m1),
    )
    brief_n = _scalar(
        conn,
        "select count(*)::int from briefs where created_at >= %s and created_at < %s",
        (m0, m1),
    )
    draft_n = _scalar(
        conn,
        "select count(*)::int from content_items where status in ('drafting', 'review')",
    )
    publish_n = _scalar(
        conn,
        "select count(*)::int from publish_events"
        " where published_at >= %s and published_at < %s",
        (m0, m1),
    )
    verify_n = _scalar(
        conn,
        "select count(*)::int from verify_events where at >= %s and at < %s",
        (m0, m1),
    )

    tracked_queries = _scalar(conn, "select count(*)::int from tracked_queries where active")
    leads_week = _scalar(
        conn,
        "select count(*)::int from booked_leads where occurred_at >= %s and occurred_at < %s",
        (w0, w1),
    )
    latest_gsc_final = _scalar(
        conn, "select max(date) from gsc_ingest_log where final"
    )

    receipt_row = conn.execute(
        "select count(*)::int as n, max(period) as latest_period from site_deltas"
    ).fetchone()

    prompts_tracked = _scalar(conn, "select count(*)::int from tracked_prompts where active")
    runs_week = _scalar(
        conn,
        "select count(*)::int from citation_runs where created_at >= %s and created_at < %s",
        (w0, w1),
    )
    samples = conn.execute(
        "select count(*) filter (where error is null)::int as ok,"
        " count(*) filter (where error is not null)::int as err"
        " from citation_results where sampled_at >= %s and sampled_at < %s",
        (w0, w1),
    ).fetchone()

    if prompts_tracked == 0:
        prove_note = "No prompts tracked yet — Gate-1 capture starts when prompts are added."
    elif runs_week == 0:
        prove_note = "No citation runs this week yet."
    else:
        prove_note = None

    counts_by_stage: dict[str, tuple[dict, str | None]] = {
        "audit": (
            {"audits_this_month": audits_n, "median_score": median_score},
            audit_note,
        ),
        "compare": (
            {"comparative_audits_this_month": compare_n},
            "No competitor comparisons this month yet." if compare_n == 0 else None,
        ),
        "brief": (
            {"briefs_this_month": brief_n},
            "No briefs written this month yet." if brief_n == 0 else None,
        ),
        "draft": (
            {"drafts_in_flight": draft_n},
            "No drafts in flight right now." if draft_n == 0 else None,
        ),
        "publish": (
            {"publish_events_this_month": publish_n},
            "Nothing published this month yet." if publish_n == 0 else None,
        ),
        "verify": (
            {"verify_events_this_month": verify_n},
            "No publish verifications this month yet." if verify_n == 0 else None,
        ),
        "measure": (
            {
                "tracked_queries": tracked_queries,
                "booked_leads_this_week": leads_week,
                "latest_gsc_final": latest_gsc_final,
            },
            "No final Search Console day ingested yet." if latest_gsc_final is None else None,
        ),
        "receipt": (
            {
                "receipts_assembled": receipt_row["n"],
                "latest_period": receipt_row["latest_period"],
            },
            "No receipts assembled yet — the monthly receipt job runs on the 1st."
            if receipt_row["n"] == 0 else None,
        ),
        "prove": (
            {
                "prompts_tracked": prompts_tracked,
                "runs_this_week": runs_week,
                "samples_ok": samples["ok"],
                "samples_err": samples["err"],
            },
            prove_note,
        ),
    }
    stages = [
        {"id": sid, "label": label, "caption": caption,
         "counts": counts_by_stage[sid][0], "note": counts_by_stage[sid][1]}
        for sid, label, caption in STAGE_DEFS
    ]

    queue_open = {
        r["kind"]: r["n"]
        for r in conn.execute(
            "select kind, count(*)::int as n from queue_items where status = 'open'"
            " group by kind order by kind"
        ).fetchall()
    }

    next_jobs = [
        {
            "job_type": r["job_type"],
            "site": r["site"],
            "next_run_at": r["next_run_at"],
            "eta_minutes": int(max(0.0, (r["next_run_at"] - now).total_seconds()) // 60),
        }
        for r in conn.execute(
            "select sc.job_type, s.domain_norm as site, sc.next_run_at"
            " from schedules sc left join sites s on s.id = sc.site_id"
            " where sc.enabled order by sc.next_run_at asc limit 10"
        ).fetchall()
    ]

    return {
        "sites": {"total": sites["total"], "control": sites["control"]},
        "stages": stages,
        "queue_open_by_kind": queue_open,
        "next_jobs": next_jobs,
    }


def sites_overview_data(conn: psycopg.Connection) -> list[dict]:
    """One row per site: domain, org, control flag, active tracked queries and
    prompts, competitor count, last page-audit grade + when (None = never
    audited — the UI renders the honest sentence), and its schedules."""
    sites = conn.execute(
        "select s.id, s.domain_norm as domain, o.name as org, s.is_control,"
        " coalesce(array_length(s.competitor_domains, 1), 0)::int as competitors"
        " from sites s join orgs o on o.id = s.org_id"
        " order by o.name, s.domain_norm"
    ).fetchall()
    queries = {
        r["site_id"]: r["n"]
        for r in conn.execute(
            "select site_id, count(*)::int as n from tracked_queries where active"
            " group by site_id"
        ).fetchall()
    }
    prompts = {
        r["site_id"]: r["n"]
        for r in conn.execute(
            "select site_id, count(*)::int as n from tracked_prompts where active"
            " group by site_id"
        ).fetchall()
    }
    last_audit = {
        r["site_id"]: r
        for r in conn.execute(
            "select distinct on (site_id) site_id,"
            " scores->>'overall_grade' as grade, coalesce(finished_at, created_at) as at"
            " from audits where status = 'done' and draft_id is null"
            " and coalesce(gate_state, '') not in ('competitor_reference', 'group_rollup')"
            " order by site_id, created_at desc"
        ).fetchall()
    }
    schedules: dict = {}
    for r in conn.execute(
        "select site_id, job_type, every_minutes, next_run_at, enabled"
        " from schedules order by job_type"
    ).fetchall():
        schedules.setdefault(r["site_id"], []).append(
            {
                "job_type": r["job_type"],
                "every_minutes": r["every_minutes"],
                "next_run_at": r["next_run_at"],
                "enabled": r["enabled"],
            }
        )
    out = []
    for s in sites:
        la = last_audit.get(s["id"])
        out.append(
            {
                "site_id": str(s["id"]),
                "domain": s["domain"],
                "org": s["org"],
                "is_control": s["is_control"],
                "tracked_queries": queries.get(s["id"], 0),
                "tracked_prompts": prompts.get(s["id"], 0),
                "competitors": s["competitors"],
                "last_audit": None if la is None else {"grade": la["grade"], "at": la["at"]},
                "schedules": schedules.get(s["id"], []),
            }
        )
    return out


def jobs_recent_data(conn: psycopg.Connection, *, limit: int = 50) -> list[dict]:
    """The last `limit` jobs, any status, newest first (limit clamped 1..200)."""
    limit = max(1, min(int(limit), 200))
    return conn.execute(
        "select j.id, j.type, j.status, j.attempts, j.max_attempts, j.last_error,"
        " j.run_after, j.created_at, j.finished_at, s.domain_norm as site"
        " from jobs j left join sites s on s.id = j.site_id"
        " order by j.created_at desc limit %s",
        (limit,),
    ).fetchall()


def _normalize_at_stake_fn():
    """Lazy accessor for gm.intel.detectors.normalize_at_stake (WP-J, built in
    parallel). Its absence must not stop the console: queue_data falls back to
    raw at_stake JSON plus an honest note. Resolved at call time (D0's
    _rank_movement_fn pattern) so tests can monkeypatch it and a later-deployed
    normalizer is picked up without a restart."""
    try:
        from gm.intel.detectors import normalize_at_stake
    except ImportError:
        return None
    return normalize_at_stake


def queue_data(conn: psycopg.Connection) -> dict:
    """queue_items by kind/status plus item rows with a unified `display`
    presentation of at_stake (WP-J's normalize_at_stake). When the normalizer
    is not deployed, display=None and a top-level note says so — the UI shows
    the raw payload rather than inventing a rendering."""
    fn = _normalize_at_stake_fn()
    summary = conn.execute(
        "select kind, status, count(*)::int as n from queue_items"
        " group by kind, status order by kind, status"
    ).fetchall()
    rows = conn.execute(
        "select q.id, q.kind, q.status, q.target, q.at_stake, q.first_seen,"
        " q.last_seen, q.snooze_until, s.domain_norm as site"
        " from queue_items q left join sites s on s.id = q.site_id"
        " order by (q.status = 'open') desc, q.last_seen desc limit 200"
    ).fetchall()
    items = []
    for r in rows:
        display = None
        if fn is not None:
            try:
                display = fn({"kind": r["kind"], "target": r["target"],
                              "at_stake": r["at_stake"]})
            except Exception:
                display = None  # one bad row must not take the console down
        items.append(
            {
                "id": str(r["id"]),
                "site": r["site"],
                "kind": r["kind"],
                "status": r["status"],
                "target": r["target"],
                "at_stake": r["at_stake"],
                "display": display,
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "snooze_until": r["snooze_until"],
            }
        )
    return {
        "summary": summary,
        "items": items,
        "normalizer_available": fn is not None,
        "note": None if fn is not None else "display normalizer not deployed",
    }


def citations_summary_data(conn: psycopg.Connection, *, now: dt.datetime | None = None) -> dict:
    """Recent citation runs, per-prompt cited/mentioned rates pooled across
    engines, and Gate-1 progress per treatment site.

    Rate honesty (gate1-thresholds rule): error samples are excluded from the
    numerator AND the denominator; a zero denominator makes the rate None.
    Gate-1 split: a site's done runs before its FIRST levers.applied_at are
    baseline, on/after are treatment; with no lever logged every run is
    baseline and treatment reads "no lever logged yet" (None, not 0/3).
    """
    now = _norm_now(now)
    recent_runs = [
        {**r, "id": str(r["id"])}
        for r in conn.execute(
            "select r.id, s.domain_norm as site, r.status, r.scheduled_for,"
            " r.started_at, r.finished_at,"
            " count(cr.*) filter (where cr.error is null)::int as samples_ok,"
            " count(cr.*) filter (where cr.error is not null)::int as samples_err"
            " from citation_runs r"
            " left join sites s on s.id = r.site_id"
            " left join citation_results cr on cr.run_id = r.id"
            " group by r.id, s.domain_norm"
            " order by r.created_at desc limit 20"
        ).fetchall()
    ]

    prompt_rates = []
    for r in conn.execute(
        "select p.id as prompt_id, p.prompt, s.domain_norm as site,"
        " count(cr.*) filter (where cr.error is null)::int as samples,"
        " count(cr.*) filter (where cr.error is null and cr.cited)::int as cited,"
        " count(cr.*) filter (where cr.error is null and cr.mentioned)::int as mentioned"
        " from tracked_prompts p"
        " join sites s on s.id = p.site_id"
        " left join citation_results cr on cr.prompt_id = p.id"
        " where p.active"
        " group by p.id, p.prompt, s.domain_norm"
        " order by s.domain_norm, p.created_at"
    ).fetchall():
        n = r["samples"]
        prompt_rates.append(
            {
                "prompt_id": str(r["prompt_id"]),
                "prompt": r["prompt"],
                "site": r["site"],
                "samples": n,
                "cited_rate": (r["cited"] / n) if n else None,
                "mentioned_rate": (r["mentioned"] / n) if n else None,
            }
        )

    gate_sites = []
    for r in conn.execute(
        "select s.domain_norm as site, fl.first_lever_at,"
        " count(r.*) filter (where r.status = 'done' and (fl.first_lever_at is null"
        "   or (r.scheduled_for at time zone 'UTC')::date < fl.first_lever_at))::int"
        "   as baseline_done,"
        " count(r.*) filter (where r.status = 'done' and fl.first_lever_at is not null"
        "   and (r.scheduled_for at time zone 'UTC')::date >= fl.first_lever_at)::int"
        "   as treatment_done"
        " from sites s"
        " left join lateral (select min(l.applied_at) as first_lever_at"
        "   from levers l where l.site_id = s.id) fl on true"
        " left join citation_runs r on r.site_id = s.id"
        " where not s.is_control"
        " group by s.domain_norm, fl.first_lever_at"
        " order by s.domain_norm"
    ).fetchall():
        no_lever = r["first_lever_at"] is None
        gate_sites.append(
            {
                "site": r["site"],
                "first_lever_at": r["first_lever_at"],
                "baseline_done": r["baseline_done"],
                "baseline_target": GATE1_RUNS_TARGET,
                "treatment_done": None if no_lever else r["treatment_done"],
                "treatment_target": GATE1_RUNS_TARGET,
                "note": "no lever logged yet" if no_lever else None,
            }
        )

    return {
        "recent_runs": recent_runs,
        "prompt_rates": prompt_rates,
        "gate1": {
            "verdict_date": GATE1_VERDICT_DATE.isoformat(),
            "days_to_verdict": (GATE1_VERDICT_DATE - now.date()).days,
            "sites": gate_sites,
        },
    }


# ---------------------------------------------------------------------------
# JSON endpoints (thin wrappers; read-only work ends with rollback)
# ---------------------------------------------------------------------------

@router.get("/admin/overview", dependencies=_admin)
def admin_overview() -> dict:
    with _connect() as conn:
        data = overview_data(conn)
        conn.rollback()
    return data


@router.get("/admin/sites/overview", dependencies=_admin)
def admin_sites_overview() -> list[dict]:
    with _connect() as conn:
        data = sites_overview_data(conn)
        conn.rollback()
    return data


@router.get("/admin/jobs/recent", dependencies=_admin)
def admin_jobs_recent(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        data = jobs_recent_data(conn, limit=limit)
        conn.rollback()
    return data


@router.get("/admin/queue", dependencies=_admin)
def admin_queue() -> dict:
    with _connect() as conn:
        data = queue_data(conn)
        conn.rollback()
    return data


@router.get("/admin/citations/summary", dependencies=_admin)
def admin_citations_summary() -> dict:
    with _connect() as conn:
        data = citations_summary_data(conn)
        conn.rollback()
    return data


@router.get("/admin/ui", response_class=HTMLResponse)
def admin_ui() -> HTMLResponse:
    """The console shell: no header guard (browser navigation cannot send one)
    but the same 404-when-unset posture; zero tenant data in the HTML."""
    if not os.environ.get("ADMIN_TOKEN"):
        raise HTTPException(status_code=404)
    return HTMLResponse(CONSOLE_HTML, headers=CONSOLE_HEADERS)


# ---------------------------------------------------------------------------
# The shell. One self-contained HTML string: no build step, no external
# CDNs/fonts/images (CSP-safe), system font stack, dark AND light via
# prefers-color-scheme. Visual language follows the "Luminous Module Atlas"
# playbook: quiet tinted canvas, per-stage gradient jewel-orbs themed via
# --g1/--g2 CSS vars, depth from two-layer shadows + blurred auras (no hard
# borders), ambient/reactive/entrance motion gated behind
# prefers-reduced-motion. Icons are hand-drawn inline SVG <symbol>s (stroke,
# currentColor) referenced with same-document <use> — still zero external URLs.
# Every dynamic value is rendered via textContent — never innerHTML with data.
# ---------------------------------------------------------------------------

CONSOLE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>growthmonk · operator console</title>
<style>
  * { box-sizing: border-box; }
  :root {
    color-scheme: light dark;
    --brand: #6366f1; --brand-ink: #4f46e5; --teal: #14b8a6;
    --canvas-a: #f7f9fc; --canvas-b: #eef2f7; --surface: #ffffff; --inset: #f1f5f9;
    --ink: #0f172a; --ink-soft: #475569; --ink-faint: #94a3b8;
    --hairline: rgb(15 23 42 / .07); --edge: rgb(15 23 42 / .045);
    --track: rgb(15 23 42 / .08); --row-hover: rgb(99 102 241 / .045);
    --nav-bg: rgb(247 249 252 / .78);
    --mut-bg: rgb(100 116 139 / .1); --mut-ink: #64748b;
    --ok-ink: #047857; --ok-bg: rgb(16 185 129 / .13);
    --run-ink: #0369a1; --run-bg: rgb(56 189 248 / .16);
    --warn-ink: #b45309; --warn-bg: rgb(245 158 11 / .14);
    --err-ink: #be123c; --err-bg: rgb(244 63 94 / .12);
    --glow-a: rgb(99 102 241 / .06); --glow-b: rgb(20 184 166 / .06);
    --shadow-card: 0 1px 2px rgb(15 23 42 / .04), 0 4px 16px rgb(15 23 42 / .05);
    --shadow-lift: 0 18px 44px rgb(15 23 42 / .14);
    --aurora-o: .5;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --brand-ink: #a5b4fc;
      --canvas-a: #0d1526; --canvas-b: #0b1220; --surface: #0f172a; --inset: #1e293b;
      --ink: #e2e8f0; --ink-soft: #94a3b8; --ink-faint: #64748b;
      --hairline: rgb(148 163 184 / .08); --edge: rgb(148 163 184 / .07);
      --track: rgb(148 163 184 / .14); --row-hover: rgb(99 102 241 / .09);
      --nav-bg: rgb(11 18 32 / .72);
      --mut-bg: rgb(148 163 184 / .13); --mut-ink: #94a3b8;
      --ok-ink: #34d399; --ok-bg: rgb(16 185 129 / .16);
      --run-ink: #38bdf8; --run-bg: rgb(56 189 248 / .16);
      --warn-ink: #fbbf24; --warn-bg: rgb(245 158 11 / .15);
      --err-ink: #fb7185; --err-bg: rgb(244 63 94 / .16);
      --glow-a: rgb(99 102 241 / .1); --glow-b: rgb(20 184 166 / .07);
      --shadow-card: 0 1px 2px rgb(2 6 23 / .5), 0 4px 18px rgb(2 6 23 / .42);
      --shadow-lift: 0 18px 44px rgb(2 6 23 / .6);
      --aurora-o: .38;
    }
  }
  [hidden] { display: none !important; }
  html { scroll-behavior: smooth; }
  html { background: var(--canvas-b); }
  body {
    margin: 0; color: var(--ink); min-height: 100vh;
    font: 14px/1.6 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background:
      radial-gradient(1200px 600px at 100% 0, var(--glow-a), transparent 60%),
      radial-gradient(900px 500px at 0% 100%, var(--glow-b), transparent 55%),
      linear-gradient(180deg, var(--canvas-a), var(--canvas-b));
  }
  .aurora-wrap {
    position: absolute; top: 0; left: 0; right: 0; height: 300px;
    overflow: hidden; pointer-events: none; z-index: 0;
  }
  .aurora {
    position: absolute; top: -70px; left: 50%; width: min(1100px, 140vw); height: 280px;
    filter: blur(46px); opacity: var(--aurora-o);
    background:
      radial-gradient(420px 200px at 18% 30%, rgb(99 102 241 / .45), transparent 60%),
      radial-gradient(380px 220px at 60% 12%, rgb(20 184 166 / .38), transparent 62%),
      radial-gradient(360px 200px at 86% 38%, rgb(139 92 246 / .35), transparent 60%);
    transform: translate3d(-50%, 0, 0);
    animation: drift 16s ease-in-out infinite alternate;
  }
  @keyframes drift {
    0% { transform: translate3d(-50%, -2%, 0) scale(1); }
    50% { transform: translate3d(-50%, 2%, 0) scale(1.06); }
    100% { transform: translate3d(-50%, -1%, 0) scale(1.03); }
  }
  nav {
    position: sticky; top: 0; z-index: 50; background: var(--nav-bg);
    -webkit-backdrop-filter: blur(14px) saturate(1.6);
    backdrop-filter: blur(14px) saturate(1.6);
    box-shadow: 0 1px 0 var(--hairline);
  }
  nav .in {
    max-width: 1160px; margin: 0 auto; padding: 10px 24px;
    display: flex; gap: 4px; align-items: center; flex-wrap: wrap;
  }
  .brand { display: flex; align-items: baseline; gap: 8px; margin-right: 16px; }
  .brand .word {
    font-size: 15.5px; font-weight: 800; letter-spacing: -.02em;
    background: linear-gradient(100deg, var(--brand), var(--teal));
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  .brand .mark {
    font-size: 10.5px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .14em; color: var(--ink-faint);
  }
  nav a {
    color: var(--ink-soft); text-decoration: none; font-size: 13px; font-weight: 600;
    padding: 5px 11px; border-radius: 999px;
    transition: color .2s, background-color .2s;
  }
  nav a:hover { color: var(--ink); background: var(--mut-bg); }
  nav a.on { color: var(--brand-ink); background: rgb(99 102 241 / .12); }
  .grow { flex: 1 1 auto; }
  .live {
    display: inline-flex; align-items: center; gap: 7px; margin: 0 12px;
    font-size: 10.5px; font-weight: 700; letter-spacing: .14em;
    text-transform: uppercase; color: var(--ink-faint);
  }
  .live .dot {
    width: 8px; height: 8px; border-radius: 50%; background: #10b981;
    animation: breathe 2.4s ease-in-out infinite;
  }
  @keyframes breathe {
    0%, 100% { box-shadow: 0 0 0 0 rgb(16 185 129 / .45); }
    50% { box-shadow: 0 0 0 5px rgb(16 185 129 / 0); }
  }
  #last-ok {
    font-size: 11px; color: var(--ink-faint); margin-right: 10px;
    font-variant-numeric: tabular-nums;
  }
  button {
    font: inherit; font-size: 12.5px; font-weight: 700; padding: 6px 15px;
    border: 0; border-radius: 999px; cursor: pointer;
    color: var(--ink-soft); background: var(--mut-bg);
    transition: transform .22s cubic-bezier(.2,.7,.2,1), box-shadow .22s,
      background-color .22s, color .22s;
  }
  button:hover {
    color: #fff; background: linear-gradient(135deg, var(--brand), #4f46e5);
    box-shadow: 0 6px 16px rgb(99 102 241 / .35); transform: translateY(-1px);
  }
  button:active { transform: scale(.97); }
  button:disabled { opacity: .55; cursor: default; pointer-events: none; }
  a:focus-visible, button:focus-visible, input:focus-visible {
    outline: 2px solid var(--brand); outline-offset: 2px;
  }
  main { max-width: 1160px; margin: 0 auto; padding: 36px 24px 110px;
    position: relative; z-index: 1; }
  section { margin: 0 0 72px; scroll-margin-top: 76px; }
  .sec-head { display: flex; align-items: center; gap: 14px; margin: 0 0 6px; }
  h2 { font-size: 21px; font-weight: 800; letter-spacing: -.02em; margin: 0; }
  h3 { font-size: 15px; font-weight: 700; letter-spacing: -.01em; margin: 30px 0 4px; }
  .eyebrow {
    font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .12em; color: var(--brand-ink);
  }
  .sub { color: var(--ink-soft); font-size: 13px; margin: 0 0 16px; max-width: 760px; }
  .mut { color: var(--ink-faint); }
  .lede { font-size: 14px; color: var(--ink-soft); margin: 0 0 26px; }
  .orb {
    display: grid; place-items: center; flex: none; color: #fff;
    background: linear-gradient(135deg, var(--g1), var(--g2));
    box-shadow: 0 6px 16px color-mix(in srgb, var(--g2) 45%, transparent),
      inset 0 1px 1px rgb(255 255 255 / .45);
    transition: transform .3s cubic-bezier(.2,.7,.2,1);
  }
  .orb.lg { width: 46px; height: 46px; border-radius: 15px; }
  .orb.md { width: 36px; height: 36px; border-radius: 12px; }
  .orb.sm { width: 30px; height: 30px; border-radius: 10px; }
  .orb svg { display: block; }
  .aura {
    position: absolute; top: -40px; left: -32px; width: 160px; height: 160px;
    border-radius: 50%; filter: blur(8px); opacity: .13; pointer-events: none;
    background: radial-gradient(circle, var(--g1), transparent 68%);
    transition: opacity .3s, transform .3s;
  }
  .kpis {
    display: grid; gap: 14px; margin: 20px 0 34px;
    grid-template-columns: repeat(auto-fit, minmax(165px, 1fr));
  }
  .kpi {
    position: relative; overflow: hidden; padding: 15px 17px 13px;
    background: var(--surface); border-radius: 16px;
    box-shadow: var(--shadow-card), inset 0 0 0 1px var(--edge);
  }
  .kpi .k {
    font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .11em; color: var(--ink-faint); margin: 10px 0 1px;
  }
  .kpi .v {
    font-size: 29px; font-weight: 800; letter-spacing: -.02em; line-height: 1.15;
    font-variant-numeric: tabular-nums;
  }
  .kpi .s {
    font-size: 12px; color: var(--ink-faint); margin-top: 2px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .loop-title { margin: 2px 0 4px; font-size: 17px; font-weight: 800; }
  .stages {
    display: grid; gap: 16px; margin-top: 18px;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  }
  .stage {
    position: relative; overflow: hidden; display: flex; flex-direction: column;
    background: var(--surface); border-radius: 18px; padding: 18px 18px 16px;
    box-shadow: var(--shadow-card), inset 0 0 0 1px var(--edge);
    transition: transform .25s cubic-bezier(.2,.7,.2,1), box-shadow .25s;
  }
  .stage:hover {
    transform: translateY(-4px);
    box-shadow: var(--shadow-lift), inset 0 0 0 1px var(--edge);
  }
  .stage:hover .orb { transform: scale(1.06) rotate(-3deg); }
  .stage:hover .aura { opacity: .32; transform: scale(1.1); }
  .s-head { display: flex; align-items: center; gap: 13px; margin: 0 0 12px; }
  .s-head h3 { margin: 0; font-size: 16px; font-weight: 800; }
  .stage .cap {
    margin: 0 0 12px; color: var(--ink-soft); font-size: 13px; line-height: 1.45;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
    overflow: hidden; min-height: 56px;
  }
  .kvs { margin-bottom: auto; }
  .kv {
    display: flex; justify-content: space-between; gap: 14px;
    font-size: 12.5px; padding: 2.5px 0;
  }
  .kv .k { color: var(--ink-faint); }
  .kv .v { font-weight: 600; font-variant-numeric: tabular-nums; }
  .note {
    display: flex; gap: 8px; align-items: flex-start; margin: 12px 0 0;
    padding: 7px 11px; border-radius: 11px; font-size: 12px; line-height: 1.45;
    color: var(--warn-ink); background: var(--warn-bg);
  }
  .note::before {
    content: ""; flex: none; width: 6px; height: 6px; margin-top: 5px;
    border-radius: 50%; background: currentColor; opacity: .7;
  }
  .pill-note {
    display: inline-block; padding: 2px 10px; border-radius: 999px;
    font-size: 11.5px; font-weight: 600; color: var(--warn-ink);
    background: var(--warn-bg); white-space: nowrap;
  }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 16px; }
  .chip {
    display: inline-flex; align-items: center; gap: 8px; padding: 4px 13px;
    border-radius: 999px; background: var(--mut-bg);
    font-size: 12.5px; font-weight: 600;
  }
  .chip .n {
    color: var(--brand-ink); font-weight: 700; font-variant-numeric: tabular-nums;
  }
  .card {
    position: relative; background: var(--surface); border-radius: 16px;
    box-shadow: var(--shadow-card), inset 0 0 0 1px var(--edge);
    overflow-x: auto; margin: 10px 0 18px;
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th {
    text-align: left; font-size: 11px; text-transform: uppercase;
    letter-spacing: .09em; color: var(--ink-faint); font-weight: 700;
    padding: 11px 14px; border-bottom: 1px solid var(--hairline); white-space: nowrap;
  }
  td { padding: 9px 14px; vertical-align: top; border-top: 1px solid var(--hairline); }
  tbody tr:first-child td { border-top: 0; }
  tbody tr { transition: background-color .15s; }
  tbody tr:hover td { background: var(--row-hover); }
  td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
  td .hl { font-weight: 600; }
  td .raw {
    font-size: 11.5px; word-break: break-all;
    font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
  }
  .badge {
    display: inline-block; padding: 2px 10px; border-radius: 999px;
    font-size: 10.5px; font-weight: 700; letter-spacing: .06em;
    text-transform: uppercase; line-height: 1.7; white-space: nowrap;
    color: var(--mut-ink); background: var(--mut-bg);
  }
  .badge.ok { color: var(--ok-ink); background: var(--ok-bg); }
  .badge.run { color: var(--run-ink); background: var(--run-bg); }
  .badge.warn { color: var(--warn-ink); background: var(--warn-bg); }
  .badge.err { color: var(--err-ink); background: var(--err-bg); }
  .empty {
    color: var(--ink-faint); font-size: 13px; padding: 28px 20px; text-align: center;
    background: var(--surface); border-radius: 14px;
    box-shadow: var(--shadow-card), inset 0 0 0 1px var(--edge); margin: 10px 0 18px;
  }
  .empty-line { color: var(--ink-faint); font-size: 13px; margin: 8px 0 16px; }
  .stat-row { display: flex; flex-wrap: wrap; gap: 14px; margin: 14px 0 20px; }
  .stat {
    position: relative; overflow: hidden; flex: 0 1 280px; min-width: 220px;
    background: var(--surface); border-radius: 16px; padding: 15px 17px;
    box-shadow: var(--shadow-card), inset 0 0 0 1px var(--edge);
    display: flex; gap: 14px; align-items: center;
  }
  .stat .v {
    font-size: 27px; font-weight: 800; letter-spacing: -.02em; line-height: 1.1;
    font-variant-numeric: tabular-nums;
  }
  .stat .s { font-size: 12px; color: var(--ink-faint); margin-top: 1px; }
  .segs {
    display: inline-flex; gap: 4px; vertical-align: middle; margin-right: 9px;
  }
  .seg { width: 22px; height: 8px; border-radius: 4px; background: var(--track); }
  .seg.fl {
    background: linear-gradient(90deg, #818cf8, #4338ca);
    box-shadow: 0 2px 6px rgb(67 56 202 / .35);
  }
  .rail-wrap { max-width: 560px; margin: 8px 0 4px; }
  .rail { position: relative; height: 10px; border-radius: 999px; background: var(--track); }
  .rail .fl {
    height: 100%; border-radius: 999px;
    background: linear-gradient(90deg, #34d399, #0d9488);
  }
  .rail .fl.warn { background: linear-gradient(90deg, #f59e0b, #d97706); }
  .rail .fl.err { background: linear-gradient(90deg, #f43f5e, #e11d48); }
  .rail .cap-mark {
    position: absolute; top: -3px; right: 0; width: 2px; height: 16px;
    border-radius: 2px; background: var(--ink-faint);
  }
  .pbars { display: grid; gap: 10px; margin: 12px 0 18px; max-width: 760px; }
  .pbar-row {
    display: grid; grid-template-columns: 140px 1fr 70px 90px;
    gap: 12px; align-items: center; font-size: 13px;
  }
  .pbar-row .nm {
    font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .pbar { height: 12px; border-radius: 999px; background: var(--track); overflow: hidden; }
  .pbar .fl {
    height: 100%; border-radius: 999px; min-width: 6px;
    background: linear-gradient(90deg, #34d399, #0d9488);
    box-shadow: inset 0 1px 1px rgb(255 255 255 / .35);
  }
  .pbar-row .ev {
    text-align: right; color: var(--ink-faint); font-size: 12px;
    font-variant-numeric: tabular-nums; white-space: nowrap;
  }
  .pbar-row .ct { text-align: right; font-weight: 700; font-variant-numeric: tabular-nums; }
  #gate {
    position: relative; overflow: hidden; max-width: 420px; margin: 13vh auto 0;
    background: var(--surface); border-radius: 22px; padding: 34px 32px 30px;
    box-shadow: var(--shadow-card), var(--shadow-lift), inset 0 0 0 1px var(--edge);
    display: flex; flex-direction: column; gap: 14px;
  }
  #gate .aura { width: 200px; height: 200px; }
  #gate h1 { font-size: 21px; font-weight: 800; letter-spacing: -.02em; margin: 0; }
  #gate p { margin: 0; font-size: 13px; color: var(--ink-soft); }
  #gate input {
    font: inherit; padding: 11px 14px; border-radius: 12px; border: 0;
    background: var(--inset); color: var(--ink);
    box-shadow: inset 0 1px 3px rgb(15 23 42 / .07), inset 0 0 0 1px var(--hairline);
  }
  #gate input:focus { outline: 2px solid var(--brand); outline-offset: 1px; }
  #gate-btn {
    padding: 11px 14px; border-radius: 12px; font-size: 13.5px; color: #fff;
    background: linear-gradient(135deg, #6366f1, #4f46e5);
    box-shadow: 0 6px 16px rgb(79 70 229 / .4), inset 0 1px 1px rgb(255 255 255 / .35);
  }
  #gate-btn:hover {
    background: linear-gradient(135deg, #6d70f4, #5652e8); transform: translateY(-1px);
    box-shadow: 0 10px 24px rgb(79 70 229 / .45),
      inset 0 1px 1px rgb(255 255 255 / .35);
  }
  .sched div { font-size: 12px; white-space: nowrap; }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } }
  .in-a { animation: rise .3s cubic-bezier(.2,.7,.2,1) both; }
  @media (max-width: 640px) {
    nav .in { padding: 10px 16px; }
    main { padding: 28px 16px 90px; }
    .kpi .v { font-size: 24px; }
    .pbar-row { grid-template-columns: 100px 1fr 84px; }
    .pbar-row .ev { display: none; }
  }
  @media (prefers-reduced-motion: reduce) {
    html { scroll-behavior: auto; }
    *, *::before, *::after {
      animation-duration: .01ms !important; animation-iteration-count: 1 !important;
      transition-duration: .01ms !important;
    }
  }
</style>
</head>
<body>
<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">
  <symbol id="i-audit" viewBox="0 0 24 24">
    <path d="M15 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7z"/>
    <path d="M15 3v4h4"/>
    <circle cx="11.3" cy="12.3" r="2.6"/>
    <path d="m13.2 14.2 2.3 2.3"/>
  </symbol>
  <symbol id="i-compare" viewBox="0 0 24 24">
    <path d="M4 20h16"/><path d="M8 20v-9"/><path d="M16 20V5"/>
  </symbol>
  <symbol id="i-brief" viewBox="0 0 24 24">
    <path d="M12 20h9"/>
    <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>
  </symbol>
  <symbol id="i-draft" viewBox="0 0 24 24">
    <path d="m12 2 8.5 4.5L12 11 3.5 6.5Z"/>
    <path d="m3.5 11.5 8.5 4.5 8.5-4.5"/>
    <path d="m3.5 16.5 8.5 4.5 8.5-4.5"/>
  </symbol>
  <symbol id="i-publish" viewBox="0 0 24 24">
    <path d="m6 9 6-6 6 6"/><path d="M12 3v13"/><path d="M5 21h14"/>
  </symbol>
  <symbol id="i-verify" viewBox="0 0 24 24">
    <path d="M12 3 5 6v5c0 4.4 2.9 8 7 10 4.1-2 7-5.6 7-10V6Z"/>
    <path d="m9 12 2 2 4-4"/>
  </symbol>
  <symbol id="i-measure" viewBox="0 0 24 24">
    <path d="M3 12h4l3-7 4 14 3-7h4"/>
  </symbol>
  <symbol id="i-receipt" viewBox="0 0 24 24">
    <path d="M6 3h12v18l-2-1.6L14 21l-2-1.6L10 21l-2-1.6L6 21Z"/>
    <path d="M9.5 8h5"/><path d="M9.5 12h5"/>
  </symbol>
  <symbol id="i-prove" viewBox="0 0 24 24">
    <path d="M12 3c.6 4.6 2.4 6.4 7 7-4.6.6-6.4 2.4-7 7-.6-4.6-2.4-6.4-7-7 4.6-.6 6.4-2.4 7-7Z"/>
  </symbol>
  <symbol id="i-globe" viewBox="0 0 24 24">
    <circle cx="12" cy="12" r="9"/><path d="M3 12h18"/>
    <path d="M12 3c2.5 2.6 4 5.6 4 9s-1.5 6.4-4 9c-2.5-2.6-4-5.6-4-9s1.5-6.4 4-9Z"/>
  </symbol>
  <symbol id="i-clock" viewBox="0 0 24 24">
    <circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>
  </symbol>
  <symbol id="i-inbox" viewBox="0 0 24 24">
    <path d="M21 12v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-6l3-7h12Z"/>
    <path d="M3 12h5l1.5 3h5L16 12h5"/>
  </symbol>
  <symbol id="i-coin" viewBox="0 0 24 24">
    <rect x="3" y="6.5" width="18" height="11" rx="2"/>
    <circle cx="12" cy="12" r="2.6"/>
    <path d="M6.5 9.8v.01"/><path d="M17.5 14.2v.01"/>
  </symbol>
  <symbol id="i-zap" viewBox="0 0 24 24">
    <path d="M13 2 4.5 14H11l-1 8L18.5 10H12Z"/>
  </symbol>
  <symbol id="i-lock" viewBox="0 0 24 24">
    <rect x="5" y="10.5" width="14" height="9.5" rx="2.5"/>
    <path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/>
  </symbol>
</svg>
<div class="aurora-wrap" aria-hidden="true"><div class="aurora"></div></div>
<nav><div class="in">
  <span class="brand">
    <span class="word">growthmonk</span>
    <span class="mark">operator console</span>
  </span>
  <a href="#overview">Overview</a>
  <a href="#sites">Sites</a>
  <a href="#jobs">Jobs</a>
  <a href="#queue">Queue</a>
  <a href="#citations">Citations</a>
  <a href="#spend">Spend</a>
  <span class="grow"></span>
  <span class="live"><span class="dot"></span>live · prod</span>
  <span id="last-ok"></span>
  <button id="refresh" type="button">Refresh</button>
</div></nav>

<div id="gate" hidden style="--g1:#6366f1;--g2:#4f46e5">
  <span class="aura" aria-hidden="true"></span>
  <span class="orb lg" aria-hidden="true">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <use href="#i-lock"/></svg>
  </span>
  <div>
    <div class="eyebrow">operator access</div>
    <h1>Unlock the console</h1>
  </div>
  <p id="gate-msg">Paste the admin token to open the console.</p>
  <input id="gate-input" type="password" autocomplete="off" placeholder="admin token">
  <button id="gate-btn" type="button">Unlock</button>
</div>

<main id="app" hidden>
  <section id="overview">
    <div class="eyebrow">live status</div>
    <h2>Overview</h2>
    <p class="sub">The whole engine at a glance — every number on this page is live
      from the database.</p>
    <div id="ov-kpis" class="kpis"></div>
    <p id="ov-lede" class="lede" hidden></p>
    <div class="eyebrow">the engine</div>
    <h3 class="loop-title">Nine stages, one loop</h3>
    <p class="sub">What this machine does to every page, left to right — from first
      audit to logged proof. Every count below is live.</p>
    <div id="ov-flow" class="stages"></div>
    <h3>Open opportunities</h3>
    <p class="sub">Detector output waiting for an operator decision
      (details in the Queue section).</p>
    <div id="ov-queue"></div>
    <h3>What runs next</h3>
    <p class="sub">The next scheduled jobs, soonest first.</p>
    <div id="ov-next"></div>
  </section>

  <section id="sites">
    <div class="sec-head">
      <span class="orb md" style="--g1:#14b8a6;--g2:#0d9488" aria-hidden="true">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <use href="#i-globe"/></svg>
      </span>
      <div>
        <div class="eyebrow">coverage</div>
        <h2>Sites</h2>
      </div>
    </div>
    <p class="sub">Every tracked domain: what we watch for it, when it was last
      audited, and what is scheduled.</p>
    <div id="sites-body"></div>
  </section>

  <section id="jobs">
    <div class="sec-head">
      <span class="orb md" style="--g1:#8b5cf6;--g2:#7c3aed" aria-hidden="true">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <use href="#i-zap"/></svg>
      </span>
      <div>
        <div class="eyebrow">the engine room</div>
        <h2>Jobs</h2>
      </div>
    </div>
    <p class="sub">The work queue behind everything above — the last 50 runs,
      plus anything dead that needs a retry.</p>
    <div id="jobs-body"></div>
  </section>

  <section id="queue">
    <div class="sec-head">
      <span class="orb md" style="--g1:#f59e0b;--g2:#d97706" aria-hidden="true">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <use href="#i-inbox"/></svg>
      </span>
      <div>
        <div class="eyebrow">worth doing next</div>
        <h2>Opportunity queue</h2>
      </div>
    </div>
    <p class="sub">What the detectors think is worth doing next, and what is
      at stake if we do it.</p>
    <div id="queue-body"></div>
  </section>

  <section id="citations">
    <div class="sec-head">
      <span class="orb md" style="--g1:#818cf8;--g2:#4338ca" aria-hidden="true">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <use href="#i-prove"/></svg>
      </span>
      <div>
        <div class="eyebrow">the proof</div>
        <h2>Citations &amp; Gate-1</h2>
      </div>
    </div>
    <p class="sub">Do AI engines cite the client when asked? Baseline runs,
      then a lever, then treatment runs — counting down to the Sep 1 verdict.</p>
    <div id="citations-body"></div>
  </section>

  <section id="spend">
    <div class="sec-head">
      <span class="orb md" style="--g1:#34d399;--g2:#0d9488" aria-hidden="true">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <use href="#i-coin"/></svg>
      </span>
      <div>
        <div class="eyebrow">cost control</div>
        <h2>Spend</h2>
      </div>
    </div>
    <p class="sub">What the data providers cost, the live DataForSEO balance,
      and the monthly budget rail.</p>
    <div id="spend-body"></div>
  </section>
</main>

<script>
(function () {
  "use strict";
  var LS_KEY = "gm_admin_token";
  var SVGNS = "http://www.w3.org/2000/svg";
  var token = null;
  try { token = localStorage.getItem(LS_KEY); } catch (e) { token = null; }

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function emptyP(msg) { return el("p", "empty", msg); }
  function setLoading(node) {
    clear(node);
    node.appendChild(el("p", "empty-line", "Loading…"));
  }

  function icon(name, size) {
    var s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("width", size); s.setAttribute("height", size);
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor");
    s.setAttribute("stroke-width", "2"); s.setAttribute("stroke-linecap", "round");
    s.setAttribute("stroke-linejoin", "round"); s.setAttribute("aria-hidden", "true");
    var u = document.createElementNS(SVGNS, "use");
    u.setAttribute("href", "#i-" + name);
    s.appendChild(u);
    return s;
  }
  function orb(name, cls, size) {
    var o = el("span", "orb " + cls);
    o.setAttribute("aria-hidden", "true");
    o.appendChild(icon(name, size));
    return o;
  }
  function aura() {
    var a = el("span", "aura");
    a.setAttribute("aria-hidden", "true");
    return a;
  }
  function setG(node, g) {
    node.style.setProperty("--g1", g[0]);
    node.style.setProperty("--g2", g[1]);
  }

  // the playbook palette: a cool spine with two warm anchors (publish, receipt)
  var G = {
    indigo: ["#6366f1", "#4f46e5"], cyan: ["#22d3ee", "#0891b2"],
    violet: ["#8b5cf6", "#7c3aed"], sky: ["#38bdf8", "#2563eb"],
    rose: ["#f43f5e", "#e11d48"], teal: ["#14b8a6", "#0d9488"],
    emerald: ["#34d399", "#0d9488"], amber: ["#f59e0b", "#d97706"],
    deep: ["#818cf8", "#4338ca"]
  };
  var STAGE_META = {
    audit: { g: G.indigo, icon: "audit" },
    compare: { g: G.cyan, icon: "compare" },
    brief: { g: G.violet, icon: "brief" },
    draft: { g: G.sky, icon: "draft" },
    publish: { g: G.rose, icon: "publish" },
    verify: { g: G.teal, icon: "verify" },
    measure: { g: G.emerald, icon: "measure" },
    receipt: { g: G.amber, icon: "receipt" },
    prove: { g: G.deep, icon: "prove" }
  };

  function fmtCount(key, v) {
    if (v === null || v === undefined) return "no data yet";
    if (key === "median_score") return Number(v).toFixed(1);
    if (key === "latest_gsc_final") return fmtDate(v);
    if (key === "latest_period") return String(v);
    return Number(v).toLocaleString();
  }
  function fmtDate(v) {
    if (!v) return "—";
    var d = new Date(v);
    if (isNaN(d)) return String(v);
    return d.toLocaleDateString(undefined,
      { year: "numeric", month: "short", day: "numeric" });
  }
  function fmtWhen(v) {
    if (!v) return "—";
    var d = new Date(v);
    if (isNaN(d)) return String(v);
    return d.toLocaleString(undefined,
      { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function fmtEta(m) {
    if (m <= 0) return "due now";
    if (m < 60) return "in " + m + "m";
    if (m < 1440) return "in " + Math.floor(m / 60) + "h " + (m % 60) + "m";
    return "in " + Math.floor(m / 1440) + "d";
  }
  function money(cents) {
    if (cents === null || cents === undefined) return "no data yet";
    return "$" + (Number(cents) / 100).toFixed(2);
  }
  function num(v) {
    return (v === null || v === undefined) ? "—" : Number(v).toLocaleString();
  }
  function trunc(s, n) {
    if (s === null || s === undefined || s === "") return "—";
    s = String(s);
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }
  function cadence(m) {
    if (m === 1440) return "daily";
    if (m === 10080) return "weekly";
    if (m === 43200) return "monthly";
    return "every " + m + "m";
  }
  function statusClass(s) {
    s = String(s || "").toLowerCase();
    if (["done", "ok", "verified", "published", "measured"].indexOf(s) >= 0) return "ok";
    if (["failed", "dead", "broken", "verify_failed", "error", "revoked"].indexOf(s) >= 0)
      return "err";
    if (["running", "started", "in_progress"].indexOf(s) >= 0) return "run";
    if (["dismissed", "disabled", "na", "abandoned"].indexOf(s) >= 0) return "";
    return "warn";
  }
  function badge(s) { return el("span", "badge " + statusClass(s), s); }

  // cols: [{h, cls, render(row) -> Node|string|null}]
  function table(cols, rows) {
    var t = el("table"), thead = el("thead"), trh = el("tr");
    cols.forEach(function (c) { trh.appendChild(el("th", c.cls, c.h)); });
    thead.appendChild(trh); t.appendChild(thead);
    var tb = el("tbody");
    rows.forEach(function (r) {
      var tr = el("tr");
      cols.forEach(function (c) {
        var td = el("td", c.cls), v = c.render(r);
        if (v instanceof Node) td.appendChild(v);
        else td.textContent = (v === null || v === undefined) ? "—" : String(v);
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    return t;
  }
  function card(node) { var c = el("div", "card"); c.appendChild(node); return c; }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = { "X-Admin-Token": token || "" };
    return fetch(path, opts).then(function (r) {
      if (r.status === 404) { var e = new Error("not found"); e.notFound = true; throw e; }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function dropToken() {
    token = null;
    try { localStorage.removeItem(LS_KEY); } catch (e) { /* private mode */ }
  }
  function showGate(msg) {
    $("app").hidden = true;
    $("gate").hidden = false;
    $("gate-msg").textContent = msg || "Paste the admin token to open the console.";
    $("gate-input").focus();
  }
  function unlock() {
    var v = $("gate-input").value.trim();
    if (!v) return;
    token = v;
    try { localStorage.setItem(LS_KEY, v); } catch (e) { /* private mode */ }
    boot();
  }
  // Any console data endpoint answering 404 means the token is not accepted
  // (or the surface is off) — clear it and re-prompt. /admin/spend is the one
  // exception, handled in loadSpend: by then the token has already been
  // proven against /admin/overview, so its 404 means "not wired yet".
  function guard(err, bodyId) {
    if (err.notFound) {
      dropToken();
      showGate("That token stopped working — paste it again.");
      return;
    }
    var b = $(bodyId);
    clear(b);
    b.appendChild(emptyP("Could not load this section: " + err.message));
  }

  var COUNT_LABELS = {
    audits_this_month: "audits this month",
    median_score: "median score",
    comparative_audits_this_month: "comparisons this month",
    briefs_this_month: "briefs this month",
    drafts_in_flight: "drafts in flight",
    publish_events_this_month: "published this month",
    verify_events_this_month: "verifications this month",
    tracked_queries: "tracked queries",
    booked_leads_this_week: "booked leads this week",
    latest_gsc_final: "latest final GSC day",
    receipts_assembled: "receipts assembled",
    latest_period: "latest period",
    prompts_tracked: "prompts tracked",
    runs_this_week: "runs this week",
    samples_ok: "samples ok",
    samples_err: "samples failed"
  };

  function kpiCard(o, i) {
    var c = el("div", "kpi in-a");
    setG(c, o.g);
    c.style.animationDelay = (i * 45) + "ms";
    c.appendChild(aura());
    c.appendChild(orb(o.icon, "sm", 15));
    c.appendChild(el("div", "k", o.k));
    var v = el("div", "v", o.v);
    if (o.vid) v.id = o.vid;
    c.appendChild(v);
    var s = el("div", "s", o.s);
    if (o.vid) s.id = o.vid + "-s";
    c.appendChild(s);
    return c;
  }
  function setKpi(vid, v, s) {
    var vn = $(vid), sn = $(vid + "-s");
    if (vn) vn.textContent = v;
    if (sn && s !== undefined) sn.textContent = s;
  }

  function renderOverview(d) {
    var lede = $("ov-lede");
    if (!d.sites.total) {
      lede.hidden = false;
      lede.textContent = "No sites yet — add the first with: gm site add <domain>.";
    } else {
      lede.hidden = true;
    }
    var byId = {};
    d.stages.forEach(function (st) { byId[st.id] = st; });
    var qOpen = 0;
    Object.keys(d.queue_open_by_kind).forEach(function (k) {
      qOpen += d.queue_open_by_kind[k];
    });
    var audits = byId.audit ? byId.audit.counts.audits_this_month : null;
    var runs = byId.prove ? byId.prove.counts.runs_this_week : null;
    var kw = $("ov-kpis");
    clear(kw);
    [
      { icon: "globe", g: G.teal, k: "sites tracked", v: num(d.sites.total),
        s: d.sites.total ? num(d.sites.control) + " control" : "none tracked yet" },
      { icon: "audit", g: G.indigo, k: "audits this month", v: num(audits),
        s: "client pages scored" },
      { icon: "prove", g: G.violet, k: "runs this week", v: num(runs),
        s: "citation panel runs" },
      { icon: "clock", g: G.rose, k: "verdict countdown", v: "—",
        s: "days to the Sep 1 verdict", vid: "kpi-verdict" },
      { icon: "inbox", g: G.amber, k: "open queue items", v: num(qOpen),
        s: "awaiting an operator call" },
      { icon: "coin", g: G.emerald, k: "spend this month", v: "—",
        s: "all providers", vid: "kpi-spend" }
    ].forEach(function (o, i) { kw.appendChild(kpiCard(o, i)); });

    var flow = $("ov-flow");
    clear(flow);
    d.stages.forEach(function (st, i) {
      var meta = STAGE_META[st.id] || { g: G.deep, icon: "prove" };
      var c = el("article", "stage in-a");
      setG(c, meta.g);
      c.style.animationDelay = (i * 45) + "ms";
      c.appendChild(aura());
      var head = el("div", "s-head");
      head.appendChild(orb(meta.icon, "lg", 22));
      var ht = el("div");
      ht.appendChild(el("span", "eyebrow", "stage 0" + (i + 1)));
      ht.appendChild(el("h3", null, st.label));
      head.appendChild(ht);
      c.appendChild(head);
      c.appendChild(el("p", "cap", st.caption));
      var kvs = el("div", "kvs");
      Object.keys(st.counts).forEach(function (k) {
        var row = el("div", "kv");
        row.appendChild(el("span", "k", COUNT_LABELS[k] || k));
        row.appendChild(el("span", "v", fmtCount(k, st.counts[k])));
        kvs.appendChild(row);
      });
      c.appendChild(kvs);
      if (st.note) c.appendChild(el("p", "note", st.note));
      flow.appendChild(c);
    });

    var qk = $("ov-queue");
    clear(qk);
    var kinds = Object.keys(d.queue_open_by_kind);
    if (!kinds.length) {
      qk.appendChild(el("p", "empty-line",
        "The opportunity queue is empty — detectors add work as measurement data lands."));
    } else {
      var chips = el("div", "chips");
      kinds.forEach(function (k) {
        var ch = el("span", "chip");
        ch.appendChild(el("span", null, k));
        ch.appendChild(el("span", "n", d.queue_open_by_kind[k]));
        chips.appendChild(ch);
      });
      qk.appendChild(chips);
    }
    var nj = $("ov-next");
    clear(nj);
    if (!d.next_jobs.length) {
      nj.appendChild(el("p", "empty-line",
        "Nothing scheduled — default schedules appear when a site is onboarded."));
    } else {
      nj.appendChild(card(table([
        { h: "job", render: function (r) { return r.job_type; } },
        { h: "site", render: function (r) { return r.site; } },
        { h: "next run", render: function (r) { return fmtWhen(r.next_run_at); } },
        { h: "eta", cls: "n", render: function (r) { return fmtEta(r.eta_minutes); } }
      ], d.next_jobs)));
    }
  }

  function schedList(list) {
    if (!list.length) return "none yet";
    var w = el("div", "sched");
    list.forEach(function (s) {
      var line = s.job_type + " · " + cadence(s.every_minutes) +
        " · next " + fmtWhen(s.next_run_at) + (s.enabled ? "" : " · off");
      w.appendChild(el("div", s.enabled ? null : "mut", line));
    });
    return w;
  }

  function loadSites() {
    var b = $("sites-body");
    setLoading(b);
    api("/admin/sites/overview").then(function (rows) {
      clear(b);
      if (!rows.length) {
        b.appendChild(emptyP("No sites yet — add one with: gm site add <domain>."));
        return;
      }
      b.appendChild(card(table([
        { h: "domain", render: function (r) {
            var w = el("span");
            w.appendChild(el("strong", null, r.domain));
            if (r.is_control) {
              w.appendChild(document.createTextNode(" "));
              w.appendChild(el("span", "badge", "control"));
            }
            return w;
          } },
        { h: "org", render: function (r) { return r.org; } },
        { h: "queries", cls: "n", render: function (r) { return r.tracked_queries; } },
        { h: "prompts", cls: "n", render: function (r) { return r.tracked_prompts; } },
        { h: "competitors", cls: "n", render: function (r) { return r.competitors; } },
        { h: "last audit", render: function (r) {
            if (!r.last_audit) return el("span", "mut", "never audited");
            var g = r.last_audit.grade === null ? "no grade" : r.last_audit.grade;
            var w = el("span");
            w.appendChild(el("span", "hl", g));
            w.appendChild(document.createTextNode(" · " + fmtWhen(r.last_audit.at)));
            return w;
          } },
        { h: "schedules", render: function (r) { return schedList(r.schedules); } }
      ], rows)));
    }).catch(function (e) { guard(e, "sites-body"); });
  }

  function retryJob(id, btn) {
    btn.disabled = true;
    btn.textContent = "retrying…";
    api("/admin/jobs/" + id + "/retry", { method: "POST" })
      .then(function () { loadJobs(); })
      .catch(function () { loadJobs(); }); // 404 here = no longer dead; re-check
  }

  function loadJobs() {
    var b = $("jobs-body");
    setLoading(b);
    Promise.all([api("/admin/jobs/recent?limit=50"), api("/admin/jobs/dead")])
      .then(function (res) {
        var recent = res[0], dead = res[1];
        clear(b);
        b.appendChild(el("h3", null, "Dead jobs"));
        if (!dead.length) {
          b.appendChild(el("p", "empty-line",
            "No dead jobs — every retry lane is clear."));
        } else {
          b.appendChild(card(table([
            { h: "id", cls: "n", render: function (r) { return r.id; } },
            { h: "type", render: function (r) { return r.type; } },
            { h: "attempts", cls: "n",
              render: function (r) { return r.attempts + "/" + r.max_attempts; } },
            { h: "last error", render: function (r) { return trunc(r.last_error, 140); } },
            { h: "finished", render: function (r) {
                return fmtWhen(r.finished_at || r.created_at); } },
            { h: "", render: function (r) {
                var btn = el("button", null, "Retry");
                btn.addEventListener("click", function () { retryJob(r.id, btn); });
                return btn;
              } }
          ], dead)));
        }
        b.appendChild(el("h3", null, "Recent jobs"));
        if (!recent.length) {
          b.appendChild(emptyP(
            "No jobs have run yet — schedules will enqueue the first ones."));
        } else {
          b.appendChild(card(table([
            { h: "id", cls: "n", render: function (r) { return r.id; } },
            { h: "type", render: function (r) { return r.type; } },
            { h: "status", render: function (r) { return badge(r.status); } },
            { h: "attempts", cls: "n",
              render: function (r) { return r.attempts + "/" + r.max_attempts; } },
            { h: "site", render: function (r) { return r.site; } },
            { h: "created", render: function (r) { return fmtWhen(r.created_at); } },
            { h: "finished", render: function (r) { return fmtWhen(r.finished_at); } },
            { h: "error", render: function (r) { return trunc(r.last_error, 100); } }
          ], recent)));
        }
      }).catch(function (e) { guard(e, "jobs-body"); });
  }

  function loadQueue() {
    var b = $("queue-body");
    setLoading(b);
    api("/admin/queue").then(function (d) {
      clear(b);
      if (d.note) {
        b.appendChild(el("p", "note",
          "Display normalizer not deployed yet — raw payloads shown below."));
      }
      if (!d.items.length) {
        b.appendChild(emptyP(
          "The queue is empty — detectors add opportunities as measurement data lands."));
        return;
      }
      var chips = el("div", "chips");
      d.summary.forEach(function (s) {
        var ch = el("span", "chip");
        ch.appendChild(el("span", null, s.kind));
        ch.appendChild(el("span", "badge " + statusClass(s.status), s.status));
        ch.appendChild(el("span", "n", s.n));
        chips.appendChild(ch);
      });
      b.appendChild(chips);
      b.appendChild(card(table([
        { h: "kind", render: function (r) { return r.kind; } },
        { h: "site", render: function (r) { return r.site; } },
        { h: "status", render: function (r) { return badge(r.status); } },
        { h: "at stake", render: function (r) {
            if (r.display) {
              var w = el("div");
              w.appendChild(el("div", "hl", r.display.headline));
              if (r.display.detail) w.appendChild(el("div", "mut", r.display.detail));
              return w;
            }
            return el("code", "raw", JSON.stringify(r.at_stake));
          } },
        { h: "last seen", render: function (r) { return fmtWhen(r.last_seen); } }
      ], d.items)));
    }).catch(function (e) { guard(e, "queue-body"); });
  }

  function segCell(done, target) {
    var w = el("span");
    var segs = el("span", "segs");
    for (var i = 0; i < target; i++) {
      segs.appendChild(el("span", "seg" + (i < done ? " fl" : "")));
    }
    w.appendChild(segs);
    w.appendChild(el("span", "mut", done + " of " + target));
    return w;
  }

  function loadCitations() {
    var b = $("citations-body");
    setLoading(b);
    api("/admin/citations/summary").then(function (d) {
      clear(b);
      var g = d.gate1;
      b.appendChild(el("h3", null, "Gate-1 progress"));
      b.appendChild(el("p", "sub", "Three baseline runs, then a lever, then three " +
        "treatment runs per site — the panel is judged on verdict day."));
      var days = g.days_to_verdict;
      var row = el("div", "stat-row");
      var st = el("div", "stat in-a");
      setG(st, G.rose);
      st.appendChild(aura());
      st.appendChild(orb("clock", "md", 18));
      var tx = el("div");
      tx.appendChild(el("div", "v", Math.abs(days)));
      tx.appendChild(el("div", "s", days >= 0
        ? "days to the " + fmtDate(g.verdict_date) + " verdict"
        : "days since the " + fmtDate(g.verdict_date) + " verdict"));
      st.appendChild(tx);
      row.appendChild(st);
      b.appendChild(row);
      setKpi("kpi-verdict", String(Math.abs(days)), days >= 0
        ? "days to the Sep 1 verdict" : "days past the Sep 1 verdict");
      if (!g.sites.length) {
        b.appendChild(emptyP("No treatment sites yet — Gate-1 tracking starts " +
          "when a non-control site is added."));
      } else {
        b.appendChild(card(table([
          { h: "site", render: function (r) { return el("span", "hl", r.site); } },
          { h: "first lever", render: function (r) {
              return r.first_lever_at ? fmtDate(r.first_lever_at)
                : el("span", "mut", "no lever logged yet");
            } },
          { h: "baseline runs", render: function (r) {
              return segCell(r.baseline_done, r.baseline_target); } },
          { h: "treatment runs", render: function (r) {
              if (r.treatment_done === null) return el("span", "pill-note", r.note);
              return segCell(r.treatment_done, r.treatment_target);
            } }
        ], g.sites)));
      }
      b.appendChild(el("h3", null, "Recent runs"));
      if (!d.recent_runs.length) {
        b.appendChild(emptyP(
          "No citation runs yet — the panel captures on schedule once prompts are tracked."));
      } else {
        b.appendChild(card(table([
          { h: "run", render: function (r) { return r.id.slice(0, 8); } },
          { h: "site", render: function (r) { return r.site; } },
          { h: "status", render: function (r) { return badge(r.status); } },
          { h: "scheduled", render: function (r) { return fmtWhen(r.scheduled_for); } },
          { h: "samples ok", cls: "n", render: function (r) { return r.samples_ok; } },
          { h: "failed", cls: "n", render: function (r) { return r.samples_err; } }
        ], d.recent_runs)));
      }
      b.appendChild(el("h3", null, "Prompt rates (all engines pooled)"));
      b.appendChild(el("p", "sub",
        "Failed samples are excluded from both sides of the rate — " +
        "a rate with no clean samples reads \"no data yet\"."));
      if (!d.prompt_rates.length) {
        b.appendChild(emptyP("No prompts tracked yet."));
      } else {
        b.appendChild(card(table([
          { h: "prompt", render: function (r) { return trunc(r.prompt, 90); } },
          { h: "site", render: function (r) { return r.site; } },
          { h: "samples", cls: "n", render: function (r) { return r.samples; } },
          { h: "cited", cls: "n", render: function (r) { return pct(r.cited_rate); } },
          { h: "mentioned", cls: "n",
            render: function (r) { return pct(r.mentioned_rate); } }
        ], d.prompt_rates)));
      }
    }).catch(function (e) { guard(e, "citations-body"); });
  }
  function pct(v) {
    if (v === null || v === undefined) return "no data yet";
    return Math.round(v * 100) + "%";
  }

  function loadSpend() {
    var b = $("spend-body");
    setLoading(b);
    api("/admin/spend").then(function (d) {
      clear(b);
      renderSpend(b, d || {});
    }).catch(function (e) {
      clear(b);
      if (e.notFound) {
        setKpi("kpi-spend", "—", "not wired yet");
        b.appendChild(emptyP(
          "Spend endpoint not wired yet — this section lights up when the " +
          "WIRE package lands."));
      } else {
        b.appendChild(emptyP("Could not load spend: " + e.message));
      }
    });
  }

  function proj(bud) {
    if (bud.projected_month_cents === null || bud.projected_month_cents === undefined) {
      return " · projection: no spend yet this month";
    }
    return " · projected " + money(bud.projected_month_cents) + " this month";
  }

  function renderSpend(b, d) {
    var bal = d.balance || {}, bud = d.budget || {}, roll = d.rollup || {};
    var row = el("div", "stat-row");
    var st = el("div", "stat in-a");
    setG(st, G.emerald);
    st.appendChild(aura());
    st.appendChild(orb("coin", "md", 18));
    var tx = el("div");
    if (bal.balance === null || bal.balance === undefined) {
      var bv = el("div");
      bv.appendChild(el("span", "pill-note", "unreachable"));
      tx.appendChild(bv);
    } else {
      tx.appendChild(el("div", "v", "$" + Number(bal.balance).toFixed(2)));
    }
    tx.appendChild(el("div", "s",
      "DataForSEO balance" + (bal.note ? " — " + bal.note : "")));
    st.appendChild(tx);
    row.appendChild(st);
    b.appendChild(row);

    if (bud.cap_cents === null || bud.cap_cents === undefined) {
      var msg = (bud.note || "no cap configured") +
        " — set GM_DFS_MONTHLY_BUDGET_CENTS to add one.";
      b.appendChild(el("p", "empty-line", msg));
      if (bud.spent_cents !== null && bud.spent_cents !== undefined) {
        b.appendChild(el("p", "sub",
          "Spent this month: " + money(bud.spent_cents) + proj(bud)));
        setKpi("kpi-spend", money(bud.spent_cents), "no monthly cap configured");
      } else {
        setKpi("kpi-spend", "—", "no spend recorded yet");
      }
    } else {
      var pctn = Math.min(100,
        Math.round(100 * Number(bud.spent_cents || 0) / Number(bud.cap_cents)));
      var wrap = el("div", "rail-wrap");
      var rail = el("div", "rail");
      var cls = bud.exceeded ? "fl err" : (pctn >= 80 ? "fl warn" : "fl");
      var fill = el("div", cls);
      fill.style.width = pctn + "%";
      rail.appendChild(fill);
      rail.appendChild(el("span", "cap-mark"));
      wrap.appendChild(rail);
      b.appendChild(wrap);
      var txt = money(bud.spent_cents || 0) + " of " + money(bud.cap_cents) +
        " cap (" + pctn + "%)" + proj(bud);
      if (bud.exceeded) txt += " — cap exceeded, paid calls are refusing";
      b.appendChild(el("p", bud.exceeded ? "note" : "sub", txt));
      setKpi("kpi-spend", money(bud.spent_cents || 0),
        "of " + money(bud.cap_cents) + " monthly cap");
    }

    var windowDays = roll.window_days || 30;
    b.appendChild(el("h3", null, "By provider — last " + windowDays + " days"));
    var prov = roll.by_provider || [];
    if (!prov.length) {
      b.appendChild(emptyP("No provider spend recorded in this window."));
    } else {
      var max = 0;
      prov.forEach(function (r) { max = Math.max(max, Number(r.cost_cents) || 0); });
      var bars = el("div", "pbars");
      prov.forEach(function (r) {
        var rw = el("div", "pbar-row");
        rw.appendChild(el("span", "nm", r.provider));
        var bar = el("div", "pbar");
        var fl = el("div", "fl");
        var w = max > 0
          ? Math.max(2, Math.round(100 * (Number(r.cost_cents) || 0) / max)) : 2;
        fl.style.width = w + "%";
        bar.appendChild(fl);
        rw.appendChild(bar);
        rw.appendChild(el("span", "ev", r.events + " events"));
        rw.appendChild(el("span", "ct", money(r.cost_cents)));
        bars.appendChild(rw);
      });
      b.appendChild(bars);
      if (roll.total_cents !== null && roll.total_cents !== undefined) {
        b.appendChild(el("p", "mut", "Total: " + money(roll.total_cents)));
      }
    }
    var purp = roll.by_purpose || [];
    if (purp.length) {
      b.appendChild(el("h3", null, "By purpose"));
      b.appendChild(card(table([
        { h: "provider", render: function (r) { return r.provider; } },
        { h: "purpose", render: function (r) { return r.purpose; } },
        { h: "events", cls: "n", render: function (r) { return r.events; } },
        { h: "cost", cls: "n", render: function (r) { return money(r.cost_cents); } }
      ], purp)));
    }
    var days = roll.by_day || [];
    if (days.length) {
      b.appendChild(el("h3", null, "By day"));
      b.appendChild(card(table([
        { h: "date", render: function (r) { return fmtDate(r.date); } },
        { h: "provider", render: function (r) { return r.provider; } },
        { h: "cost", cls: "n", render: function (r) { return money(r.cost_cents); } }
      ], days)));
    }
    if (roll.last_event) {
      var le = roll.last_event;
      b.appendChild(el("p", "mut", "Last spend: " + money(le.cost_cents) + " · " +
        le.provider + " · " + le.purpose + " · " + fmtWhen(le.created_at)));
    }
  }

  function stamp() {
    $("last-ok").textContent = "updated " + new Date().toLocaleTimeString(undefined,
      { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function boot() {
    if (!token) { showGate(); return; }
    api("/admin/overview").then(function (d) {
      $("gate").hidden = true;
      $("app").hidden = false;
      stamp();
      renderOverview(d);
      loadSites();
      loadJobs();
      loadQueue();
      loadCitations();
      loadSpend();
    }).catch(function (e) {
      if (e.notFound) {
        dropToken();
        showGate("That token was not accepted — paste it again.");
      } else {
        $("gate").hidden = true;
        $("app").hidden = false;
        var b = $("ov-flow");
        clear(b);
        b.appendChild(emptyP("Could not load the overview: " + e.message));
      }
    });
  }

  // active-section highlight in the top bar
  var navLinks = {};
  var navAs = document.querySelectorAll("nav a");
  for (var ai = 0; ai < navAs.length; ai++) {
    navLinks[navAs[ai].getAttribute("href").slice(1)] = navAs[ai];
  }
  var currentSec = null;
  function setActive(id) {
    if (currentSec === id) return;
    Object.keys(navLinks).forEach(function (k) {
      navLinks[k].className = (k === id) ? "on" : "";
    });
    currentSec = id;
  }
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) setActive(en.target.id);
      });
    }, { rootMargin: "-20% 0px -65% 0px" });
    Object.keys(navLinks).forEach(function (id) {
      var sec = document.getElementById(id);
      if (sec) io.observe(sec);
    });
  }

  $("gate-btn").addEventListener("click", unlock);
  $("gate-input").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") unlock();
  });
  $("refresh").addEventListener("click", boot);
  boot();
})();
</script>
</body>
</html>
"""
