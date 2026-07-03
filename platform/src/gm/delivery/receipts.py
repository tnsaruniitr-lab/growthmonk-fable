"""Delta Receipt engine (Phase C wave 3, docs/phase-c-wave3-contracts.md).

Two artifacts:

  * content_deltas — per published content item: 28-day GSC windows pivoting on
    publish_events.published_at (after-window shifted by the 3-day GSC lag),
    FINAL gsc_daily days only, url_norm join including page_url_history, plus a
    findings diff (gm.audit.delta.audit_delta) between the latest pre-publish
    and latest post-publish page audit.
  * site_deltas — the monthly rollup a Delta Receipt renders from: audits +
    score movement, fix log (levers + published content), citation rates with
    Wilson CIs vs the prior period + control-site drift, queue actions, spend.

Honesty rules (binding): missing GSC data produces empty {} sections, never
zeros; an absent before/after audit is noted, never faked; non-comparable
findings (ADR-13: check_version changed) are reported separately and never
counted as resolved/regressed. Everything is deterministic given the rows read
— the only now() is the render footer's generated-at stamp (and row
bookkeeping defaults applied by the database).

The renderer reuses the report design system by importing gm.delivery.report
(_CSS/_esc/badge chips); only receipt-specific section markup lives here.

Phase D0 (docs/phase-d0-contracts.md): the receipt payload gains a
"rank_tracking" section via gm.intel.rank_tracker.rank_movement — lazily
imported and tolerated when absent (the module is built concurrently), and
rendered as a 'Google visibility' section (rank arrows, AI Overview citation
badges, competitor top-10 changes) before the BETA citation section, with an
honest empty state when no queries are tracked.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from gm.audit.delta import audit_delta
from gm.audit.pipeline import canonicalize_url
from gm.delivery import report
from gm.delivery.evidence import CLAIM_CEILING
from gm.infra import jobs
from gm.intel.gsc_ingest import FINAL_LAG_DAYS as GSC_LAG_DAYS
from gm.intel.variance import fmt_rate, wilson

WINDOW_DAYS = 28
# Audits that must never enter a client's own delta history (ADR from wave 2/3:
# competitor references and draft scorecards are graded in `audits` too).
_EXCLUDED_GATE_STATES = ("competitor_reference", "group_rollup", "draft")


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without DB)
# ---------------------------------------------------------------------------

def delta_windows(published: dt.date) -> tuple[dt.date, dt.date, dt.date, dt.date]:
    """28-day windows pivoting on the publish date.

    before: the 28 days ending the day BEFORE publish — pre-publish traffic
    needs no lag correction. after: 28 days starting GSC_LAG_DAYS after
    publish — GSC data inside the lag is not final yet, and the publish-day
    transition itself is not attributable. Returns (before_start, before_end,
    after_start, after_end), all inclusive.
    """
    before_end = published - dt.timedelta(days=1)
    before_start = before_end - dt.timedelta(days=WINDOW_DAYS - 1)
    after_start = published + dt.timedelta(days=GSC_LAG_DAYS)
    after_end = after_start + dt.timedelta(days=WINDOW_DAYS - 1)
    return before_start, before_end, after_start, after_end


def period_bounds(period: str) -> tuple[dt.date, dt.date]:
    """'YYYY-MM' -> [first of month, first of next month)."""
    try:
        y_s, m_s = period.split("-")
        year, month = int(y_s), int(m_s)
        start = dt.date(year, month, 1)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"period must be 'YYYY-MM', got {period!r}") from exc
    end = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    return start, end


def prior_period(period: str) -> str:
    start, _ = period_bounds(period)
    prev = start - dt.timedelta(days=1)
    return f"{prev.year:04d}-{prev.month:02d}"


def citation_entry(
    prompt_id: Any, prompt: str, before: tuple[int, int], after: tuple[int, int]
) -> dict:
    """One per-prompt receipt row: rates for both periods with Wilson CIs."""
    bk, bn = before
    ak, an = after
    b_rate = bk / bn if bn else 0.0
    a_rate = ak / an if an else 0.0
    return {
        "prompt_id": str(prompt_id),
        "prompt": prompt,
        "before": {"k": bk, "n": bn},
        "after": {"k": ak, "n": an},
        "gain": round(a_rate - b_rate, 4),
        "ci_before": [round(v, 4) for v in wilson(bk, bn)],
        "ci_after": [round(v, 4) for v in wilson(ak, an)],
    }


def _jsonable(value: Any) -> Any:
    """Recursively coerce rows into Jsonb-serializable primitives."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    return value


def _rank_movement_fn():
    """Lazy accessor for gm.intel.rank_tracker.rank_movement (phase D0).

    The rank_tracker module is built concurrently — its absence must not stop
    receipts from assembling. Returns the function, or None when unavailable.
    Called at assemble time (not import time) so tests can monkeypatch it and
    a later-deployed rank_tracker is picked up without a restart.
    """
    try:
        from gm.intel.rank_tracker import rank_movement
    except ImportError:
        return None
    return rank_movement


def _url_variants(urls: set[str]) -> list[str]:
    """Exact-match set for the gsc_daily.page join: each url with and without
    a trailing slash (GSC reports the served form; url_norm keeps path verbatim)."""
    out: set[str] = set()
    for u in urls:
        if not u:
            continue
        out.add(u)
        out.add(u.rstrip("/") if u.endswith("/") else u + "/")
    return sorted(out)


# ---------------------------------------------------------------------------
# content delta
# ---------------------------------------------------------------------------

def _gsc_window(
    conn: psycopg.Connection, site_id: Any, urls: list[str], start: dt.date, end: dt.date
) -> dict:
    """Aggregate FINAL gsc_daily rows for the url set over [start, end].

    Honest empty {}: when the window has ZERO final ingested days (per
    gsc_ingest_log) there is no data to speak of — a zero would be a lie.
    When final days exist but the page has no rows, zeros are the truth.
    """
    if not urls:
        return {}
    final_days = conn.execute(
        "select count(*) as n from gsc_ingest_log"
        " where site_id = %s and search_type = 'web' and final and date between %s and %s",
        (site_id, start, end),
    ).fetchone()["n"]
    if not final_days:
        return {}
    agg = conn.execute(
        """
        select count(distinct g.date) as days_with_data,
               coalesce(sum(g.clicks), 0) as clicks,
               coalesce(sum(g.impressions), 0) as impressions,
               coalesce(sum(g.position * g.impressions) / nullif(sum(g.impressions), 0),
                        avg(g.position)) as position
          from gsc_daily g
          join gsc_ingest_log l
            on l.site_id = g.site_id and l.date = g.date
           and l.search_type = g.search_type and l.final
         where g.site_id = %s and g.search_type = 'web'
           and g.date between %s and %s and g.page = any(%s)
        """,
        (site_id, start, end, urls),
    ).fetchone()
    top = conn.execute(
        """
        select g.query, sum(g.clicks) as clicks, sum(g.impressions) as impressions
          from gsc_daily g
          join gsc_ingest_log l
            on l.site_id = g.site_id and l.date = g.date
           and l.search_type = g.search_type and l.final
         where g.site_id = %s and g.search_type = 'web'
           and g.date between %s and %s and g.page = any(%s)
         group by g.query
         order by 2 desc, 3 desc, 1
         limit 5
        """,
        (site_id, start, end, urls),
    ).fetchall()
    clicks = int(agg["clicks"])
    impressions = int(agg["impressions"])
    position = agg["position"]
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "final_days": int(final_days),
        "days_with_data": int(agg["days_with_data"] or 0),
        "clicks": clicks,
        "impressions": impressions,
        "ctr": round(clicks / impressions, 4) if impressions else 0.0,
        "position": round(float(position), 2) if position is not None else None,
        "top_queries": [
            {"query": r["query"], "clicks": int(r["clicks"]),
             "impressions": int(r["impressions"])}
            for r in top
        ],
    }


def _page_audit(
    conn: psycopg.Connection, page_id: Any, pivot: dt.datetime, *, before: bool
) -> dict | None:
    """Latest done PAGE audit strictly before (or at/after) the pivot.

    Draft scorecards (draft_id set / gate_state='draft') and competitor
    references never qualify — they are not this page's history.
    """
    op = "<" if before else ">="
    return conn.execute(
        f"""
        select id, scores, coalesce(finished_at, created_at) as at
          from audits
         where page_id = %s and status = 'done' and draft_id is null
           and coalesce(gate_state, 'ok') != all(%s)
           and coalesce(finished_at, created_at) {op} %s
         order by coalesce(finished_at, created_at) desc
         limit 1
        """,
        (page_id, list(_EXCLUDED_GATE_STATES), pivot),
    ).fetchone()


def _audit_findings(conn: psycopg.Connection, audit_id: Any) -> list[dict]:
    return conn.execute(
        "select check_id, check_version, status from audit_findings where audit_id = %s",
        (audit_id,),
    ).fetchall()


def compute_content_delta(conn: psycopg.Connection, *, content_item_id: Any) -> str:
    """Compute (or idempotently recompute) the content_deltas row for one item.

    Windows pivot on the latest publish_events.published_at (UTC date). Upserts
    on (content_item_id, window_start) so recomputes refresh in place. Returns
    the content_deltas row id.
    """
    item = conn.execute(
        "select * from content_items where id = %s", (content_item_id,)
    ).fetchone()
    if item is None:
        raise ValueError(f"content item {content_item_id} not found")
    pub = conn.execute(
        "select * from publish_events where content_item_id = %s"
        " order by published_at desc limit 1",
        (content_item_id,),
    ).fetchone()
    if pub is None:
        raise ValueError(
            f"content item {content_item_id} has no publish event — nothing to pivot on"
        )
    published_at: dt.datetime = pub["published_at"]
    pub_date = published_at.astimezone(dt.UTC).date()
    b_start, b_end, a_start, a_end = delta_windows(pub_date)

    urls: set[str] = set()
    if pub["url"]:
        urls.add(canonicalize_url(pub["url"]))
    if item["page_id"] is not None:
        page = conn.execute(
            "select url_norm from pages where id = %s", (item["page_id"],)
        ).fetchone()
        if page:
            urls.add(page["url_norm"])
        urls.update(
            r["url_norm"]
            for r in conn.execute(
                "select url_norm from page_url_history where page_id = %s",
                (item["page_id"],),
            ).fetchall()
        )
    variants = _url_variants(urls)

    gsc_before = _gsc_window(conn, item["site_id"], variants, b_start, b_end)
    gsc_after = _gsc_window(conn, item["site_id"], variants, a_start, a_end)

    before_audit = after_audit = None
    if item["page_id"] is not None:
        before_audit = _page_audit(conn, item["page_id"], published_at, before=True)
        after_audit = _page_audit(conn, item["page_id"], published_at, before=False)
    if before_audit and after_audit:
        findings_diff: dict = audit_delta(
            _audit_findings(conn, before_audit["id"]),
            _audit_findings(conn, after_audit["id"]),
            before_scores=before_audit["scores"],
            after_scores=after_audit["scores"],
        )
        findings_diff["before_audit_id"] = str(before_audit["id"])
        findings_diff["after_audit_id"] = str(after_audit["id"])
    else:
        # Absent audits are noted, not faked (contract).
        missing = []
        if not before_audit:
            missing.append("no pre-publish page audit")
        if not after_audit:
            missing.append("no post-publish page audit")
        findings_diff = {"skipped": True, "note": "; ".join(missing)}

    row = conn.execute(
        """
        insert into content_deltas
          (org_id, content_item_id, publish_event_id, before_audit_id, after_audit_id,
           window_start, window_end, gsc_before, gsc_after, findings_diff)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (content_item_id, window_start) do update set
           publish_event_id = excluded.publish_event_id,
           before_audit_id = excluded.before_audit_id,
           after_audit_id = excluded.after_audit_id,
           window_end = excluded.window_end,
           gsc_before = excluded.gsc_before,
           gsc_after = excluded.gsc_after,
           findings_diff = excluded.findings_diff
        returning id
        """,
        (
            item["org_id"], item["id"], pub["id"],
            before_audit["id"] if before_audit else None,
            after_audit["id"] if after_audit else None,
            b_start, a_end,
            Jsonb(_jsonable(gsc_before)), Jsonb(_jsonable(gsc_after)),
            Jsonb(_jsonable(findings_diff)),
        ),
    ).fetchone()
    return str(row["id"])


# ---------------------------------------------------------------------------
# site receipt rollup
# ---------------------------------------------------------------------------

def _citation_windows(
    conn: psycopg.Connection, site_id: Any, start: dt.date, end: dt.date
) -> dict[str, dict]:
    """prompt_id -> {prompt, k, n} over error-free samples in [start, end)."""
    rows = conn.execute(
        """
        select r.prompt_id, max(p.prompt) as prompt,
               count(*) as n, count(*) filter (where r.cited) as k
          from citation_results r
          join citation_runs cr on cr.id = r.run_id
          join tracked_prompts p on p.id = r.prompt_id
         where cr.site_id = %s and r.error is null
           and r.sampled_at >= %s and r.sampled_at < %s
         group by r.prompt_id
        """,
        (site_id, start, end),
    ).fetchall()
    return {
        str(r["prompt_id"]): {"prompt": r["prompt"], "k": int(r["k"]), "n": int(r["n"])}
        for r in rows
    }


def _control_rates(
    conn: psycopg.Connection, org_id: Any, site_id: Any, start: dt.date, end: dt.date
) -> dict[str, dict]:
    """control site id -> {domain, k, n} pooled over the window."""
    rows = conn.execute(
        """
        select s.id, s.domain_norm, count(*) as n, count(*) filter (where r.cited) as k
          from citation_results r
          join citation_runs cr on cr.id = r.run_id
          join sites s on s.id = cr.site_id
         where s.org_id = %s and s.is_control and s.id <> %s and r.error is null
           and r.sampled_at >= %s and r.sampled_at < %s
         group by s.id, s.domain_norm
        """,
        (org_id, site_id, start, end),
    ).fetchall()
    return {
        str(r["id"]): {"domain": r["domain_norm"], "k": int(r["k"]), "n": int(r["n"])}
        for r in rows
    }


def assemble_site_receipt(conn: psycopg.Connection, *, site_id: Any, period: str) -> str:
    """Assemble the monthly rollup payload and upsert the site_deltas row.

    Every number is a pure function of rows; timestamps come from row data.
    Returns the site_deltas row id.
    """
    start, end = period_bounds(period)
    prior = prior_period(period)
    p_start, p_end = period_bounds(prior)

    site = conn.execute("select * from sites where id = %s", (site_id,)).fetchone()
    if site is None:
        raise ValueError(f"site {site_id} not found")
    org_id = site["org_id"]

    # -- audits + score movement (page audits only; references excluded) ------
    audits = conn.execute(
        """
        select id, scores, coalesce(finished_at, created_at) as at
          from audits
         where site_id = %s and status = 'done' and draft_id is null
           and coalesce(gate_state, 'ok') != all(%s)
           and coalesce(finished_at, created_at) >= %s
           and coalesce(finished_at, created_at) < %s
         order by coalesce(finished_at, created_at)
        """,
        (site_id, list(_EXCLUDED_GATE_STATES), start, end),
    ).fetchall()

    def _point(row: dict) -> dict:
        scores = row["scores"] if isinstance(row["scores"], dict) else {}
        return {
            "audit_id": str(row["id"]),
            "at": row["at"],
            "score": scores.get("overall_score"),
            "grade": scores.get("overall_grade"),
        }

    movement: dict[str, Any] = {"first": None, "last": None, "change": None}
    if audits:
        first, last = _point(audits[0]), _point(audits[-1])
        movement = {"first": first, "last": last, "change": None}
        if isinstance(first["score"], (int, float)) and isinstance(last["score"], (int, float)):
            movement["change"] = round(float(last["score"]) - float(first["score"]), 1)

    # -- fix log: levers + published content ----------------------------------
    levers = conn.execute(
        "select applied_at, lever_class, description from levers"
        " where site_id = %s and applied_at >= %s and applied_at < %s order by applied_at, id",
        (site_id, start, end),
    ).fetchall()
    published = conn.execute(
        """
        select pe.content_item_id, pe.target, pe.url, pe.published_at, ci.kind
          from publish_events pe
          join content_items ci on ci.id = pe.content_item_id
         where ci.site_id = %s and pe.published_at >= %s and pe.published_at < %s
         order by pe.published_at, pe.id
        """,
        (site_id, start, end),
    ).fetchall()

    # -- content deltas computed this period ----------------------------------
    deltas = conn.execute(
        """
        select cd.content_item_id, cd.window_start, cd.window_end,
               cd.gsc_before, cd.gsc_after, cd.findings_diff, ci.kind, pe.url
          from content_deltas cd
          join content_items ci on ci.id = cd.content_item_id
          left join publish_events pe on pe.id = cd.publish_event_id
         where ci.site_id = %s and cd.created_at >= %s and cd.created_at < %s
         order by cd.created_at, cd.id
        """,
        (site_id, start, end),
    ).fetchall()
    content = [
        {
            "content_item_id": str(d["content_item_id"]),
            "url": d["url"],
            "kind": d["kind"],
            "window_start": d["window_start"],
            "window_end": d["window_end"],
            "gsc_before": d["gsc_before"] or {},
            "gsc_after": d["gsc_after"] or {},
            "findings": d["findings_diff"] or {},
        }
        for d in deltas
    ]

    # -- citation rates (BETA): this period vs prior, Wilson CIs --------------
    cur = _citation_windows(conn, site_id, start, end)
    prev = _citation_windows(conn, site_id, p_start, p_end)
    prompts = [
        citation_entry(
            pid,
            (cur.get(pid) or prev.get(pid))["prompt"],  # type: ignore[index]
            (prev.get(pid, {}).get("k", 0), prev.get(pid, {}).get("n", 0)),
            (cur.get(pid, {}).get("k", 0), cur.get(pid, {}).get("n", 0)),
        )
        for pid in sorted(set(cur) | set(prev))
    ]
    ctrl_cur = _control_rates(conn, org_id, site_id, start, end)
    ctrl_prev = _control_rates(conn, org_id, site_id, p_start, p_end)
    control_sites = []
    gains = []
    for sid in sorted(set(ctrl_cur) | set(ctrl_prev)):
        c, p = ctrl_cur.get(sid), ctrl_prev.get(sid)
        entry: dict[str, Any] = {
            "domain": (c or p)["domain"],  # type: ignore[index]
            "before": {"k": p["k"], "n": p["n"]} if p else None,
            "after": {"k": c["k"], "n": c["n"]} if c else None,
            "gain": None,
        }
        if c and p and c["n"] and p["n"]:
            entry["gain"] = round(c["k"] / c["n"] - p["k"] / p["n"], 4)
            gains.append(entry["gain"])
        control_sites.append(entry)
    mean_abs_drift = round(sum(abs(g) for g in gains) / len(gains), 4) if gains else None

    # -- queue actions ----------------------------------------------------------
    actions = conn.execute(
        """
        select status, count(*) as n from queue_items
         where site_id = %s and status <> 'open'
           and last_seen >= %s and last_seen < %s
         group by status order by status
        """,
        (site_id, start, end),
    ).fetchall()
    opened = conn.execute(
        "select count(*) as n from queue_items"
        " where site_id = %s and first_seen >= %s and first_seen < %s",
        (site_id, start, end),
    ).fetchone()["n"]

    # -- spend (site-attributable: cost_events linked through the site's jobs) --
    spend_rows = conn.execute(
        """
        select ce.provider, sum(ce.cost_cents) as cents
          from cost_events ce
          join jobs j on j.id = ce.job_id
         where j.site_id = %s and ce.created_at >= %s and ce.created_at < %s
         group by ce.provider order by 2 desc, 1
        """,
        (site_id, start, end),
    ).fetchall()
    spend = {
        "total_cents": round(sum(float(r["cents"]) for r in spend_rows), 4),
        "by_provider": [
            {"provider": r["provider"], "cents": round(float(r["cents"]), 4)}
            for r in spend_rows
        ],
    }

    gsc_connected = conn.execute(
        "select 1 from connections where site_id = %s and kind = 'gsc' and status = 'ok'",
        (site_id,),
    ).fetchone() is not None

    # -- rank tracking (phase D0; module may not be deployed yet) ---------------
    movement_fn = _rank_movement_fn()
    if movement_fn is None:
        rank_tracking: dict[str, Any] = {
            "available": False,
            "queries": [],
            "note": "rank tracking not available yet",
        }
    else:
        rank_tracking = {
            "available": True,
            # since/until are inclusive dates; the period is [start, end).
            "queries": movement_fn(
                conn, site_id, since=start, until=end - dt.timedelta(days=1)
            ),
        }

    payload = _jsonable({
        "period": period,
        "period_start": start,
        "period_end": end,
        "prior_period": prior,
        "site": {
            "id": site["id"],
            "domain": site["domain_norm"],
            "is_control": site["is_control"],
        },
        "audits": {"run": len(audits), "movement": movement},
        "fix_log": {"levers": levers, "published": published},
        "content": content,
        "rank_tracking": rank_tracking,
        "citations": {
            "prompts": prompts,
            "controls": {"sites": control_sites, "mean_abs_drift": mean_abs_drift},
        },
        "queue": {
            "opened": int(opened),
            "actions": [{"status": r["status"], "n": int(r["n"])} for r in actions],
        },
        "spend": spend,
        "gsc": {"connected": gsc_connected},
    })

    row = conn.execute(
        """
        insert into site_deltas (org_id, site_id, period, payload)
        values (%s, %s, %s, %s)
        on conflict (site_id, period) do update set payload = excluded.payload
        returning id
        """,
        (org_id, site_id, period, Jsonb(payload)),
    ).fetchone()
    return str(row["id"])


# ---------------------------------------------------------------------------
# renderer (design system imported from gm.delivery.report — not forked)
# ---------------------------------------------------------------------------

# Receipt-specific additions only; the base stylesheet is report._CSS verbatim.
_RECEIPT_CSS = """
table.delta { width: 100%; border-collapse: collapse; margin: 8px 0;
  font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 13px; }
table.delta th { text-align: left; font-size: 10.5px; text-transform: uppercase;
  letter-spacing: .12em; color: #8A948F; padding: 6px 8px;
  border-bottom: 1px solid #141B1E; }
table.delta td { padding: 7px 8px; border-bottom: 1px solid #E4E1D8;
  font-variant-numeric: tabular-nums; overflow-wrap: anywhere; vertical-align: top; }
.delta-up { color: #2E7146; font-weight: 700; }
.delta-down { color: #B3402F; font-weight: 700; }
.pill.p-beta { background: #F5EBD3; color: #7C570B; }
p.honest { font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 13px; color: #5B665F; font-style: italic; }
ul.moves { list-style: none; margin: 6px 0; padding: 0; }
ul.moves li { padding: 6px 2px; border-bottom: 1px solid #E4E1D8;
  font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 13px; }
"""

_esc = report._esc  # the ONLY way dynamic values enter the document


def _stamp_html(movement: dict) -> str:
    change = report._num(movement.get("change"))
    if change is None:
        return (
            '<div class="stamp inconclusive"><div class="g">&mdash;</div>'
            '<div class="s">no data</div><div class="l">Score movement</div></div>'
        )
    first = report._dict_or_empty(movement.get("first"))
    last = report._dict_or_empty(movement.get("last"))
    span = (
        f"{report._score_cell(first.get('score'))} &rarr; "
        f"{report._score_cell(last.get('score'))}"
    )
    return (
        f'<div class="stamp"><div class="g">{_esc(f"{change:+g}")}</div>'
        f'<div class="s">{span}</div><div class="l">Score movement</div></div>'
    )


def _fix_log_html(fix_log: dict) -> list[str]:
    levers = fix_log.get("levers") if isinstance(fix_log.get("levers"), list) else []
    published = fix_log.get("published") if isinstance(fix_log.get("published"), list) else []
    out = ["<section><h2>Fix log</h2>",
           '<p class="sub">What we changed this period — levers applied and content shipped.</p>']
    if not levers and not published:
        out.append('<p class="honest">No levers or publishes recorded this period.</p>')
        out.append("</section>")
        return out
    out.append('<ol class="queue">')
    for lv in levers:
        if not isinstance(lv, dict):
            continue
        out.append(
            "<li><div>"
            f'<span class="q-name">{_esc(lv.get("lever_class"))}</span>'
            f'<div class="q-detail">{_esc(lv.get("description"))}</div></div>'
            f'<span class="q-tags"><span class="chip">{_esc(lv.get("applied_at"))}</span>'
            "</span></li>"
        )
    for pe in published:
        if not isinstance(pe, dict):
            continue
        out.append(
            "<li><div>"
            f'<span class="q-name">Published ({_esc(pe.get("kind"))})</span>'
            f'<div class="q-detail">{_esc(pe.get("url"))}</div></div>'
            '<span class="q-tags"><span class="pill p-pass">shipped</span></span></li>'
        )
    out.append("</ol></section>")
    return out


def _findings_html(content: list, checks_meta: dict | None) -> list[str]:
    out = ["<section><h2>Findings movement</h2>",
           '<p class="sub">Comparable checks only (ADR-13): a check whose version changed '
           "between audits is excluded, never counted as resolved or regressed.</p>"]
    for item in content:
        if not isinstance(item, dict):
            continue
        diff = report._dict_or_empty(item.get("findings"))
        label = item.get("url") or item.get("content_item_id") or ""
        out.append(f"<h3>{_esc(label)}</h3>")
        if diff.get("skipped") or not diff:
            note = diff.get("note") or "no audit pair for this item"
            out.append(f'<p class="honest">Findings diff skipped: {_esc(note)}.</p>')
            continue
        out.append('<ul class="moves">')
        for cid in diff.get("resolved") or []:
            name = report._meta_for(checks_meta, cid).get("name")
            out.append(
                f'<li><span class="pill p-pass">resolved</span> <code>{_esc(cid)}</code>'
                + (f" {_esc(name)}" if name else "") + "</li>"
            )
        for cid in diff.get("regressed") or []:
            name = report._meta_for(checks_meta, cid).get("name")
            out.append(
                f'<li><span class="pill p-fail">regressed</span> <code>{_esc(cid)}</code>'
                + (f" {_esc(name)}" if name else "") + "</li>"
            )
        out.append("</ul>")
        non_comp = diff.get("non_comparable") or []
        if non_comp:
            out.append(
                f'<p class="honest">{len(non_comp)} check(s) not comparable '
                "(check version changed between audits) — excluded per ADR-13.</p>"
            )
        summary = diff.get("summary")
        if summary:
            out.append(f'<p class="honest">{_esc(summary)}</p>')
    if not content:
        out.append('<p class="honest">No content deltas were computed this period.</p>')
    out.append("</section>")
    return out


def _gsc_cell(section: dict, key: str) -> str:
    v = section.get(key)
    return "&mdash;" if v is None else _esc(v)


def _gsc_html(payload: dict) -> list[str]:
    out = ["<section><h2>Search performance (GSC)</h2>"]
    gsc = report._dict_or_empty(payload.get("gsc"))
    content = payload.get("content") if isinstance(payload.get("content"), list) else []
    if not gsc.get("connected"):
        out.append(
            '<p class="honest">No GSC connection — search performance is omitted '
            "rather than guessed.</p></section>"
        )
        return out
    rows = []
    for item in content:
        if not isinstance(item, dict):
            continue
        before = report._dict_or_empty(item.get("gsc_before"))
        after = report._dict_or_empty(item.get("gsc_after"))
        label = item.get("url") or item.get("content_item_id") or ""
        if not before and not after:
            rows.append(
                f"<tr><td>{_esc(label)}</td>"
                '<td colspan="6"><span class="honest">no finalized GSC data in the '
                "measurement windows yet</span></td></tr>"
            )
            continue
        rows.append(
            f"<tr><td>{_esc(label)}</td>"
            f"<td>{_gsc_cell(before, 'clicks')}</td><td>{_gsc_cell(after, 'clicks')}</td>"
            f"<td>{_gsc_cell(before, 'impressions')}</td>"
            f"<td>{_gsc_cell(after, 'impressions')}</td>"
            f"<td>{_gsc_cell(before, 'position')}</td><td>{_gsc_cell(after, 'position')}</td>"
            "</tr>"
        )
    if not rows:
        out.append(
            '<p class="honest">GSC is connected, but no per-item measurement windows '
            "closed this period.</p>"
        )
    else:
        out.append(
            '<table class="delta"><thead><tr><th>Page</th>'
            "<th>Clicks (before)</th><th>Clicks (after)</th>"
            "<th>Impr. (before)</th><th>Impr. (after)</th>"
            "<th>Pos. (before)</th><th>Pos. (after)</th></tr></thead><tbody>"
        )
        out.extend(rows)
        out.append("</tbody></table>")
        out.append(
            '<p class="honest">28-day windows around each publish date; finalized '
            "GSC days only (3-day lag).</p>"
        )
    out.append("</section>")
    return out


def _movement_view(m: dict) -> dict:
    """Normalize one rank_movement entry into the renderer's canonical shape.

    rank_tracker is built concurrently against a prose contract ("per query:
    first/last rank + aio_cited in window, competitor top_domains changes"),
    so the exact key names are accepted defensively via documented aliases.
    """
    def pick(*keys: str) -> Any:
        for key in keys:
            value = m.get(key)
            if value is not None:
                return value
        return None

    aio_first = pick("aio_cited_first", "aio_first")
    aio_last = pick("aio_cited_last", "aio_last")
    aio = m.get("aio_cited")
    if isinstance(aio, dict):
        aio_first = aio.get("first") if aio_first is None else aio_first
        aio_last = aio.get("last") if aio_last is None else aio_last
    elif isinstance(aio, bool) and aio_last is None:
        aio_last = aio
    entered = pick("entered_top10", "competitors_entered", "entered")
    left = pick("left_top10", "competitors_left", "left")
    if entered is None or left is None:
        competitors = m.get("competitors")
        if isinstance(competitors, dict):
            entered = competitors.get("entered") if entered is None else entered
            left = competitors.get("left") if left is None else left
    return {
        "query": pick("query", "query_norm") or "",
        "first_rank": report._num(pick("first_rank", "rank_first")),
        "last_rank": report._num(pick("last_rank", "rank_last")),
        "aio_first": None if aio_first is None else bool(aio_first),
        "aio_last": None if aio_last is None else bool(aio_last),
        "entered": entered if isinstance(entered, list) else [],
        "left": left if isinstance(left, list) else [],
    }


def _rank_cell(first: float | None, last: float | None) -> str:
    """'#12 → #7' with a movement arrow; NULL ranks are honest absence."""
    def fmt(rank: float | None) -> str:
        return "&mdash;" if rank is None else f"#{_esc(round(rank))}"

    if first is None and last is None:
        return '<span class="honest">not ranked</span>'
    arrow = ""
    if last is not None and (first is None or last < first):
        arrow = ' <span class="delta-up">&#9650;</span>'
    elif first is not None and (last is None or last > first):
        arrow = ' <span class="delta-down">&#9660;</span>'
    return f"{fmt(first)} &rarr; {fmt(last)}{arrow}"


def _aio_cell(first: bool | None, last: bool | None) -> str:
    if last:
        badge = '<span class="pill p-pass">AIO cited</span>'
        return badge + (" (gained)" if first is False else "")
    if first:
        return '<span class="delta-down">lost AIO citation</span>'
    return "&mdash;"


def _competitor_cell(entered: list, left: list) -> str:
    parts = []
    if entered:
        parts.append("entered top-10: " + ", ".join(_esc(d) for d in entered))
    if left:
        parts.append("left top-10: " + ", ".join(_esc(d) for d in left))
    return " &middot; ".join(parts) if parts else "&mdash;"


def _rank_tracking_html(payload: dict) -> list[str]:
    """The 'Google visibility' section: tracked-query rank movement, AI
    Overview citations, competitor top-10 changes. Honest empty states."""
    tracking = report._dict_or_empty(payload.get("rank_tracking"))
    queries = tracking.get("queries") if isinstance(tracking.get("queries"), list) else []
    out = [
        "<section><h2>Google visibility</h2>",
        '<p class="sub">Tracked-query rank movement this period, AI Overview '
        "citations, and competitor changes in the top 10.</p>",
    ]
    if not tracking.get("available"):
        out.append('<p class="honest">Rank tracking is not enabled for this site yet.</p>')
        out.append("</section>")
        return out
    if not queries:
        out.append(
            '<p class="honest">No tracked queries this period — rank movement '
            "appears once queries are tracked.</p>"
        )
        out.append("</section>")
        return out
    out.append(
        '<table class="delta"><thead><tr><th>Query</th><th>Rank</th>'
        "<th>AI Overview</th><th>Competitor changes</th></tr></thead><tbody>"
    )
    for entry in queries:
        if not isinstance(entry, dict):
            continue
        view = _movement_view(entry)
        out.append(
            f"<tr><td>{_esc(view['query'])}</td>"
            f"<td>{_rank_cell(view['first_rank'], view['last_rank'])}</td>"
            f"<td>{_aio_cell(view['aio_first'], view['aio_last'])}</td>"
            f"<td>{_competitor_cell(view['entered'], view['left'])}</td></tr>"
        )
    out.append("</tbody></table>")
    out.append(
        '<p class="honest">First vs last tracked check inside the period; '
        "a &mdash; rank means the site was not in the tracked depth.</p>"
    )
    out.append("</section>")
    return out


def _ci_txt(ci: Any) -> str:
    if not isinstance(ci, (list, tuple)) or len(ci) != 2:
        return ""
    lo, hi = report._num(ci[0]), report._num(ci[1])
    if lo is None or hi is None:
        return ""
    return f"CI {lo:.2f}&ndash;{hi:.2f}"


def _citations_html(payload: dict) -> list[str]:
    citations = report._dict_or_empty(payload.get("citations"))
    prompts = citations.get("prompts") if isinstance(citations.get("prompts"), list) else []
    controls = report._dict_or_empty(citations.get("controls"))
    out = [
        '<section><h2>AI citation rates <span class="pill p-beta">BETA</span></h2>',
        '<p class="sub">Share of engine runs that cited the site, this period vs prior. '
        "Wilson 95% intervals; small samples read wide on purpose.</p>",
    ]
    if not prompts:
        out.append('<p class="honest">No citation samples recorded in either period.</p>')
        out.append("</section>")
        return out
    out.append(
        '<table class="delta"><thead><tr><th>Prompt</th><th>This period</th>'
        "<th>Prior period</th><th>Gain</th></tr></thead><tbody>"
    )
    for p in prompts:
        if not isinstance(p, dict):
            continue
        after = report._dict_or_empty(p.get("after"))
        before = report._dict_or_empty(p.get("before"))
        ak, an = int(after.get("k") or 0), int(after.get("n") or 0)
        bk, bn = int(before.get("k") or 0), int(before.get("n") or 0)
        gain = report._num(p.get("gain"))
        gain_cls = "delta-up" if (gain or 0) > 0 else ("delta-down" if (gain or 0) < 0 else "")
        out.append(
            f"<tr><td>{_esc(p.get('prompt'))}</td>"
            f"<td>named in {_esc(fmt_rate(ak, an))} runs <span class=\"honest\">"
            f"{_ci_txt(p.get('ci_after'))}</span></td>"
            f"<td>was {_esc(fmt_rate(bk, bn))} <span class=\"honest\">"
            f"{_ci_txt(p.get('ci_before'))}</span></td>"
            f'<td><span class="{gain_cls}">'
            + ("&mdash;" if gain is None else _esc(f"{gain:+.2f}"))
            + "</span></td></tr>"
        )
    out.append("</tbody></table>")
    ctrl_sites = controls.get("sites") if isinstance(controls.get("sites"), list) else []
    drift = report._num(controls.get("mean_abs_drift"))
    if ctrl_sites:
        parts = []
        for c in ctrl_sites:
            if not isinstance(c, dict):
                continue
            g = report._num(c.get("gain"))
            parts.append(
                f"{_esc(c.get('domain'))} "
                + ("(insufficient samples)" if g is None else _esc(f"{g:+.2f}"))
            )
        out.append(
            '<p class="honest">Control-site drift: ' + " &middot; ".join(parts)
            + (f" &middot; mean |drift| {drift:.2f}" if drift is not None else "")
            + "</p>"
        )
    else:
        out.append('<p class="honest">No control sites tracked — drift baseline unavailable.</p>')
    out.append("</section>")
    return out


def _ops_html(payload: dict) -> list[str]:
    queue = report._dict_or_empty(payload.get("queue"))
    spend = report._dict_or_empty(payload.get("spend"))
    actions = queue.get("actions") if isinstance(queue.get("actions"), list) else []
    by_provider = (
        spend.get("by_provider") if isinstance(spend.get("by_provider"), list) else []
    )
    action_txt = ", ".join(
        f"{_esc(a.get('status'))}: {_esc(a.get('n'))}" for a in actions if isinstance(a, dict)
    ) or "none"
    total = report._num(spend.get("total_cents")) or 0.0
    provider_txt = ", ".join(
        f"{_esc(s.get('provider'))} ${(report._num(s.get('cents')) or 0.0) / 100:.2f}"
        for s in by_provider if isinstance(s, dict)
    )
    return [
        "<section><h2>Queue &amp; spend</h2>",
        '<ul class="moves">',
        f"<li>Queue items opened: {_esc(queue.get('opened') or 0)}"
        f" &middot; actions taken: {action_txt}</li>",
        f"<li>Spend (site-attributed): ${total / 100:.2f}"
        + (f" &middot; {provider_txt}" if provider_txt else "")
        + "</li>",
        "</ul></section>",
    ]


def render_receipt_html(
    site: dict, payload: dict, *, checks_meta: dict | None = None
) -> str:
    """Render a monthly Delta Receipt as a self-contained HTML document.

    Pure function of its inputs except the generated-at footer stamp. Strict
    CSP posture inherited from gm.delivery.report: inline CSS only, no scripts,
    every dynamic value escaped through report._esc.
    """
    site = report._dict_or_empty(site)
    payload = report._dict_or_empty(payload)
    domain = site.get("domain_norm") or site.get("domain") or ""
    period = payload.get("period") or ""
    audits = report._dict_or_empty(payload.get("audits"))
    movement = report._dict_or_empty(audits.get("movement"))
    content = payload.get("content") if isinstance(payload.get("content"), list) else []
    generated = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")

    out: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="robots" content="noindex">',
        f"<title>Delta Receipt — {_esc(domain)} — {_esc(period)}</title>",
        f"<style>{report._CSS}{_RECEIPT_CSS}</style>",
        "</head><body><main>",
        '<header class="masthead">',
        "<div>",
        '<p class="eyebrow">AI Demand Capture &middot; Delta Receipt</p>',
        f"<h1>{_esc(domain)}</h1>",
        f'<div class="meta">Period {_esc(period)}'
        f" &middot; {_esc(audits.get('run') or 0)} audit(s) run"
        f" &middot; prior period {_esc(payload.get('prior_period'))}</div>",
        "</div>",
        _stamp_html(movement),
        "</header>",
    ]
    out.extend(_fix_log_html(report._dict_or_empty(payload.get("fix_log"))))
    out.extend(_findings_html(content, checks_meta))
    out.extend(_gsc_html(payload))
    out.extend(_rank_tracking_html(payload))  # before the BETA citation section
    out.extend(_citations_html(payload))
    out.extend(_ops_html(payload))
    out.append(
        "<footer><span>"
        f"Delta Receipt {_esc(period)} &middot; generated {_esc(generated)}</span>"
        f'<span class="claim">Claim ceiling: {_esc(CLAIM_CEILING)}.</span>'
        "</footer>"
    )
    out.append("</main></body></html>")
    return "".join(out)


# ---------------------------------------------------------------------------
# job handlers
# ---------------------------------------------------------------------------

def handle_compute_delta(ctx: jobs.JobContext) -> None:
    """Job 'compute_delta' payload {content_item_id}: compute the delta, then
    advance the item to 'measured' (only from published/verified — a failed or
    abandoned item never silently becomes measured)."""
    content_item_id = ctx.job.payload.get("content_item_id")
    if not content_item_id:
        raise ValueError("compute_delta payload requires content_item_id")
    compute_content_delta(ctx.conn, content_item_id=content_item_id)
    ctx.conn.execute(
        "update content_items set status = 'measured', updated_at = now()"
        " where id = %s and status in ('published', 'verified')",
        (content_item_id,),
    )


def handle_assemble_receipt(ctx: jobs.JobContext) -> None:
    """Job 'assemble_receipt' payload {period: 'YYYY-MM'}: build the rollup.

    The period is explicit in the payload — deriving it from now() would make
    the receipt non-deterministic and retries could land in a different month.
    """
    period = ctx.job.payload.get("period")
    if not period:
        raise ValueError("assemble_receipt payload requires period 'YYYY-MM'")
    if ctx.job.site_id is None:
        raise ValueError("assemble_receipt requires job.site_id")
    assemble_site_receipt(ctx.conn, site_id=ctx.job.site_id, period=period)
