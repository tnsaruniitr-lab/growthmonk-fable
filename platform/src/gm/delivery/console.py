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
# prefers-color-scheme, restrained palette with status-only color accents.
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
    --bg: #f7f7f5; --panel: #ffffff; --ink: #1d1f1e; --muted: #6a716d;
    --line: #e3e5e1; --soft: #eef0ec;
    --ok: #177a3d; --warn: #96690a; --err: #b3261e;
    --ok-soft: #e2f2e7; --warn-soft: #f7eed6; --err-soft: #fae5e3;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #131514; --panel: #1b1e1c; --ink: #e7e9e6; --muted: #98a09b;
      --line: #2c302d; --soft: #232725;
      --ok: #57b784; --warn: #d5a441; --err: #ee7f74;
      --ok-soft: #1c2f24; --warn-soft: #322a13; --err-soft: #38201d;
    }
  }
  [hidden] { display: none !important; }
  html { scroll-behavior: smooth; }
  body { margin: 0; background: var(--bg); color: var(--ink);
    font: 14px/1.6 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  .num, td.n { font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
    font-variant-numeric: tabular-nums; }
  .mut { color: var(--muted); }
  nav { position: sticky; top: 0; z-index: 10; background: var(--bg);
    border-bottom: 1px solid var(--line); }
  nav .in { max-width: 1120px; margin: 0 auto; padding: 12px 24px; display: flex;
    gap: 16px; align-items: center; flex-wrap: wrap; }
  nav .brand { font-weight: 700; margin-right: 8px; }
  nav a { color: var(--muted); text-decoration: none; font-size: 13px; }
  nav a:hover { color: var(--ink); }
  .grow { flex: 1 1 auto; }
  main { max-width: 1120px; margin: 0 auto; padding: 40px 24px 96px; }
  section { margin: 0 0 64px; scroll-margin-top: 72px; }
  h2 { font-size: 18px; margin: 0 0 4px; letter-spacing: -.01em; }
  h3 { font-size: 14px; margin: 32px 0 4px; }
  .sub { color: var(--muted); font-size: 13px; margin: 0 0 16px; }
  .lede { font-size: 15px; margin: 0 0 24px; }
  .flow { display: flex; flex-wrap: wrap; gap: 16px 32px; counter-reset: stage; }
  .stage { flex: 1 1 300px; position: relative; background: var(--panel);
    border: 1px solid var(--line); border-radius: 10px; padding: 16px; }
  .stage:not(:last-child)::after { content: "\2192"; position: absolute;
    right: -24px; top: 50%; transform: translateY(-50%); color: var(--muted); }
  .stage h3 { margin: 0 0 4px; font-size: 13.5px; }
  .stage h3::before { counter-increment: stage; content: counter(stage) " · ";
    color: var(--muted); font-weight: 400; }
  .stage .cap { color: var(--muted); font-size: 12.5px; margin: 0 0 12px; }
  .kv { display: flex; justify-content: space-between; gap: 16px; font-size: 13px;
    padding: 2px 0; }
  .kv .k { color: var(--muted); }
  .kv .v { font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
    font-variant-numeric: tabular-nums; }
  .note { color: var(--warn); font-size: 12.5px; margin: 8px 0 0; }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 16px; }
  .chip { border: 1px solid var(--line); background: var(--panel);
    border-radius: 999px; padding: 2px 12px; font-size: 12.5px; }
  .card { background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; overflow-x: auto; margin: 8px 0 16px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-size: 11px; text-transform: uppercase;
    letter-spacing: .08em; color: var(--muted); font-weight: 600;
    padding: 10px 12px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  td { padding: 8px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
  tbody tr:last-child td { border-bottom: 0; }
  td.n, th.n { text-align: right; }
  td .hl { font-weight: 600; }
  td .raw { font-size: 11.5px; word-break: break-all;
    font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace; }
  .badge { display: inline-block; padding: 0 8px; border-radius: 999px;
    font-size: 11.5px; line-height: 18px; border: 1px solid var(--line);
    color: var(--muted); background: var(--soft); white-space: nowrap; }
  .badge.ok { color: var(--ok); background: var(--ok-soft); border-color: transparent; }
  .badge.warn { color: var(--warn); background: var(--warn-soft);
    border-color: transparent; }
  .badge.err { color: var(--err); background: var(--err-soft);
    border-color: transparent; }
  .empty { color: var(--muted); font-size: 13px; padding: 24px; text-align: center;
    border: 1px dashed var(--line); border-radius: 10px; margin: 8px 0 16px; }
  .empty-line { color: var(--muted); font-size: 13px; margin: 8px 0 16px; }
  button { font: inherit; font-size: 12.5px; padding: 4px 12px; border-radius: 8px;
    border: 1px solid var(--line); background: var(--panel); color: var(--ink);
    cursor: pointer; }
  button:hover { border-color: var(--muted); }
  button:disabled { opacity: .5; cursor: default; }
  #gate { max-width: 400px; margin: 96px auto; background: var(--panel);
    border: 1px solid var(--line); border-radius: 12px; padding: 32px;
    display: flex; flex-direction: column; gap: 16px; }
  #gate h1 { font-size: 18px; margin: 0; }
  #gate p { margin: 0; font-size: 13px; }
  #gate input { font: inherit; padding: 8px 12px; border-radius: 8px;
    border: 1px solid var(--line); background: var(--bg); color: var(--ink); }
  .bar { height: 8px; border-radius: 999px; background: var(--soft);
    overflow: hidden; margin: 8px 0; }
  .bar .fill { height: 100%; }
  .bar .fill.ok { background: var(--ok); }
  .bar .fill.warn { background: var(--warn); }
  .bar .fill.err { background: var(--err); }
  .stat-line { font-size: 14px; margin: 0 0 8px; }
  .sched div { font-size: 12px; white-space: nowrap; }
</style>
</head>
<body>
<nav><div class="in">
  <span class="brand">growthmonk <span class="mut">· operator console</span></span>
  <a href="#overview">Overview</a>
  <a href="#sites">Sites</a>
  <a href="#jobs">Jobs</a>
  <a href="#queue">Queue</a>
  <a href="#citations">Citations</a>
  <a href="#spend">Spend</a>
  <span class="grow"></span>
  <button id="refresh" type="button">Refresh</button>
</div></nav>

<div id="gate" hidden>
  <h1>Operator console</h1>
  <p id="gate-msg" class="mut">Paste the admin token to open the console.</p>
  <input id="gate-input" type="password" autocomplete="off" placeholder="admin token">
  <button id="gate-btn" type="button">Unlock</button>
</div>

<main id="app" hidden>
  <section id="overview">
    <h2>Overview</h2>
    <p class="sub">What this machine does, in one loop — nine stages, left to
      right. Every count below is live.</p>
    <p id="ov-sites" class="lede"></p>
    <div id="ov-flow" class="flow"></div>
    <h3>Open opportunities</h3>
    <p class="sub">Detector output waiting for an operator decision
      (details in the Queue section).</p>
    <div id="ov-queue"></div>
    <h3>What runs next</h3>
    <p class="sub">The next scheduled jobs, soonest first.</p>
    <div id="ov-next"></div>
  </section>

  <section id="sites">
    <h2>Sites</h2>
    <p class="sub">Every tracked domain: what we watch for it, when it was last
      audited, and what is scheduled.</p>
    <div id="sites-body"></div>
  </section>

  <section id="jobs">
    <h2>Jobs</h2>
    <p class="sub">The work queue behind everything above — the last 50 runs,
      plus anything dead that needs a retry.</p>
    <div id="jobs-body"></div>
  </section>

  <section id="queue">
    <h2>Opportunity queue</h2>
    <p class="sub">What the detectors think is worth doing next, and what is
      at stake if we do it.</p>
    <div id="queue-body"></div>
  </section>

  <section id="citations">
    <h2>Citations &amp; Gate-1</h2>
    <p class="sub">Do AI engines cite the client when asked? Baseline runs,
      then a lever, then treatment runs — counting down to the Sep 1 verdict.</p>
    <div id="citations-body"></div>
  </section>

  <section id="spend">
    <h2>Spend</h2>
    <p class="sub">What the data providers cost, the live DataForSEO balance,
      and the monthly budget rail.</p>
    <div id="spend-body"></div>
  </section>
</main>

<script>
(function () {
  "use strict";
  var LS_KEY = "gm_admin_token";
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
  function setLoading(node) { clear(node); node.appendChild(el("p", "empty-line", "Loading…")); }

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
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
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

  function renderOverview(d) {
    var lede = $("ov-sites");
    if (!d.sites.total) {
      lede.textContent =
        "No sites yet — add the first with: gm site add <domain>.";
    } else {
      lede.textContent = d.sites.total + (d.sites.total === 1 ? " site" : " sites") +
        " tracked · " + d.sites.control + " control";
    }
    var flow = $("ov-flow");
    clear(flow);
    d.stages.forEach(function (st) {
      var c = el("div", "stage");
      c.appendChild(el("h3", null, st.label));
      c.appendChild(el("p", "cap", st.caption));
      Object.keys(st.counts).forEach(function (k) {
        var row = el("div", "kv");
        row.appendChild(el("span", "k", COUNT_LABELS[k] || k));
        row.appendChild(el("span", "v", fmtCount(k, st.counts[k])));
        c.appendChild(row);
      });
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
        chips.appendChild(el("span", "chip", k + " · " + d.queue_open_by_kind[k]));
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
            return g + " · " + fmtWhen(r.last_audit.at);
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
        chips.appendChild(el("span", "chip", s.kind + " · " + s.status + " · " + s.n));
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

  function loadCitations() {
    var b = $("citations-body");
    setLoading(b);
    api("/admin/citations/summary").then(function (d) {
      clear(b);
      var g = d.gate1;
      b.appendChild(el("h3", null, "Gate-1 progress"));
      var when = g.days_to_verdict >= 0
        ? g.days_to_verdict + " days to the " + fmtDate(g.verdict_date) + " verdict"
        : "verdict day was " + (-g.days_to_verdict) + " days ago";
      b.appendChild(el("p", "sub", "Three baseline runs, then a lever, then " +
        "three treatment runs per site — " + when + "."));
      if (!g.sites.length) {
        b.appendChild(emptyP(
          "No treatment sites yet — Gate-1 tracking starts when a non-control site is added."));
      } else {
        b.appendChild(card(table([
          { h: "site", render: function (r) { return r.site; } },
          { h: "first lever", render: function (r) {
              return r.first_lever_at ? fmtDate(r.first_lever_at)
                : el("span", "mut", "no lever logged yet");
            } },
          { h: "baseline runs", cls: "n", render: function (r) {
              return r.baseline_done + " of " + r.baseline_target; } },
          { h: "treatment runs", cls: "n", render: function (r) {
              if (r.treatment_done === null) return el("span", "mut", r.note);
              return r.treatment_done + " of " + r.treatment_target;
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
    var line = el("p", "stat-line");
    line.appendChild(el("span", "mut", "DataForSEO balance: "));
    var balv = (bal.balance === null || bal.balance === undefined)
      ? "unreachable" : "$" + Number(bal.balance).toFixed(2);
    line.appendChild(el("strong", "num", balv));
    if (bal.note) line.appendChild(el("span", "mut", " — " + bal.note));
    b.appendChild(line);

    if (bud.cap_cents === null || bud.cap_cents === undefined) {
      var msg = (bud.note || "no cap configured") +
        " — set GM_DFS_MONTHLY_BUDGET_CENTS to add one.";
      b.appendChild(el("p", "empty-line", msg));
      if (bud.spent_cents !== null && bud.spent_cents !== undefined) {
        b.appendChild(el("p", "stat-line",
          "Spent this month: " + money(bud.spent_cents) + proj(bud)));
      }
    } else {
      var pctn = Math.min(100,
        Math.round(100 * Number(bud.spent_cents || 0) / Number(bud.cap_cents)));
      var bar = el("div", "bar");
      var cls = bud.exceeded ? "fill err" : (pctn >= 80 ? "fill warn" : "fill ok");
      var fill = el("div", cls);
      fill.style.width = pctn + "%";
      bar.appendChild(fill);
      b.appendChild(bar);
      var txt = money(bud.spent_cents || 0) + " of " + money(bud.cap_cents) +
        " cap (" + pctn + "%)" + proj(bud);
      if (bud.exceeded) txt += " — cap exceeded, paid calls are refusing";
      b.appendChild(el("p", bud.exceeded ? "note" : "mut", txt));
    }

    var windowDays = roll.window_days || 30;
    b.appendChild(el("h3", null, "By provider — last " + windowDays + " days"));
    var prov = roll.by_provider || [];
    if (!prov.length) {
      b.appendChild(emptyP("No provider spend recorded in this window."));
    } else {
      b.appendChild(card(table([
        { h: "provider", render: function (r) { return r.provider; } },
        { h: "events", cls: "n", render: function (r) { return r.events; } },
        { h: "cost", cls: "n", render: function (r) { return money(r.cost_cents); } }
      ], prov)));
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

  function boot() {
    if (!token) { showGate(); return; }
    api("/admin/overview").then(function (d) {
      $("gate").hidden = true;
      $("app").hidden = false;
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
