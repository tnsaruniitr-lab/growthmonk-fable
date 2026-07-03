"""Weekly WhatsApp lead card (Phase D1, docs/phase-d1-contracts.md).

booked_leads in -> a 4-line WhatsApp trend card out:

    headline   "Booked consults this week: 7 (▲ from 4)"
    delta      the best movement evidence available (cascade below)
    next       top open queue item by est_clicks_gain
    footer     "GrowthMonk — reply STOP to pause"

The delta line cascades through evidence sources, most specific first:
rank_tracker.rank_movement (lazy import — the module is built concurrently
and its absence must degrade, never crash) -> the most recent resolved audit
finding (content_deltas.findings_diff) -> the latest site_deltas score
movement -> an honest "no movement data yet" line.

Honesty rules (binding): every line renders an explicit empty state when its
data is absent; numbers only ever come from rows — never invented. Privacy:
raw phone numbers are never stored; the recipient wa_id lives only in
connections.meta and is never copied into cost_events units.
"""

from __future__ import annotations

import datetime as dt
import importlib
import logging
import os
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from gm.infra import costs, jobs

log = logging.getLogger(__name__)

MAX_CARD_CHARS = 1024
# 4 clipped lines + 3 newlines <= 4*250 + 3 = 1003 < 1024 by construction.
_LINE_LIMIT = 250
FOOTER = "GrowthMonk — reply STOP to pause"
SOURCES = ("whatsapp", "call", "manual", "booking_system")

WABA_TOKEN_ENV = "WABA_TOKEN"
WABA_PHONE_NUMBER_ID_ENV = "WABA_PHONE_NUMBER_ID"

# Indirection so tests can stub module resolution deterministically
# (gm.intel.rank_tracker / gm.delivery.whatsapp are concurrent builds).
_import_module = importlib.import_module

_KIND_LABELS = {
    "striking_distance": "Push a striking-distance query",
    "decay": "Rescue a decaying page",
    "ctr_outlier": "Fix a low-CTR page",
    "cannibalization": "Resolve keyword cannibalization",
    "keyword_gap": "Close a keyword gap",
}


# ---------------------------------------------------------------------------
# week math (pure)
# ---------------------------------------------------------------------------

def week_start_for(day: dt.date) -> dt.date:
    """Monday of the week containing `day` (weeks are Monday-start)."""
    return day - dt.timedelta(days=day.weekday())


def _utc_midnight(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


# ---------------------------------------------------------------------------
# lead capture + weekly stats
# ---------------------------------------------------------------------------

def add_lead(
    conn: psycopg.Connection,
    *,
    org_id: Any,
    site_id: Any,
    source: str = "manual",
    occurred_at: dt.datetime | None = None,
    notes: str | None = None,
    attribution: dict | None = None,
) -> str:
    """Insert one booked lead (operator log path). Returns the row id."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
    row = conn.execute(
        "insert into booked_leads (org_id, site_id, source, occurred_at, notes, attribution)"
        " values (%s, %s, %s, coalesce(%s, now()), %s, %s) returning id",
        (org_id, site_id, source, occurred_at, notes, Jsonb(attribution or {})),
    ).fetchone()
    return str(row["id"])


def week_stats(conn: psycopg.Connection, site_id: Any, *, week_start: dt.date) -> dict:
    """Booked-lead counts for the Monday-start week containing `week_start`.

    Returns {booked, prev_week, by_source, trend, week_start} where trend
    compares against the immediately preceding week. `week_start` is snapped
    to its Monday defensively so callers can pass any day of the week.
    """
    ws = week_start_for(week_start)
    we = ws + dt.timedelta(days=7)
    prev_ws = ws - dt.timedelta(days=7)
    rows = conn.execute(
        """
        select source,
               count(*) filter (where occurred_at >= %(ws)s and occurred_at < %(we)s) as cur,
               count(*) filter (where occurred_at >= %(pws)s and occurred_at < %(ws)s) as prev
          from booked_leads
         where site_id = %(site_id)s
           and occurred_at >= %(pws)s and occurred_at < %(we)s
         group by source
        """,
        {
            "site_id": site_id,
            "ws": _utc_midnight(ws),
            "we": _utc_midnight(we),
            "pws": _utc_midnight(prev_ws),
        },
    ).fetchall()
    booked = sum(int(r["cur"]) for r in rows)
    prev_week = sum(int(r["prev"]) for r in rows)
    by_source = {r["source"]: int(r["cur"]) for r in sorted(rows, key=lambda r: r["source"])
                 if int(r["cur"]) > 0}
    trend = "up" if booked > prev_week else ("down" if booked < prev_week else "flat")
    return {
        "booked": booked,
        "prev_week": prev_week,
        "by_source": by_source,
        "trend": trend,
        "week_start": ws,
    }


# ---------------------------------------------------------------------------
# card assembly
# ---------------------------------------------------------------------------

def _headline(stats: dict) -> str:
    booked, prev = stats["booked"], stats["prev_week"]
    if booked == 0 and prev == 0:
        return "No booked consults logged this week (none the week before either)."
    if booked == 0:
        return f"No booked consults logged this week (▼ from {prev})."
    if stats["trend"] == "up":
        return f"Booked consults this week: {booked} (▲ from {prev})"
    if stats["trend"] == "down":
        return f"Booked consults this week: {booked} (▼ from {prev})"
    return f"Booked consults this week: {booked} (= {prev} last week)"


def _rank_of(move: dict, which: str) -> int | None:
    """Defensive rank extraction — rank_movement's row shape is a concurrent
    build; probe the plausible key spellings and nested forms."""
    for key in (f"{which}_rank", f"rank_{which}", which):
        v = move.get(key)
        if isinstance(v, dict):
            v = v.get("rank")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    return None


def _rank_delta_line(
    conn: psycopg.Connection, site_id: Any, *, since: dt.date, until: dt.date
) -> str | None:
    """Best Google rank improvement this week via gm.intel.rank_tracker.

    The module is being built concurrently: absence, a missing function, or a
    runtime error all degrade to None (the cascade continues) — never a crash.
    """
    try:
        tracker = _import_module("gm.intel.rank_tracker")
    except ImportError:
        return None
    try:
        moves = tracker.rank_movement(conn, site_id, since=since, until=until)
    except Exception:
        log.warning("rank_tracker.rank_movement failed; falling back", exc_info=True)
        return None
    best: tuple[int, str, int, int] | None = None
    entered: tuple[int, str] | None = None
    for move in moves or []:
        if not isinstance(move, dict):
            continue
        query = move.get("query") or move.get("query_norm")
        if not query:
            continue
        first, last = _rank_of(move, "first"), _rank_of(move, "last")
        if last is None:
            continue
        if first is not None and last < first:
            gain = first - last
            if best is None or gain > best[0]:
                best = (gain, str(query), first, last)
        elif first is None and (entered is None or last < entered[0]):
            entered = (last, str(query))
    if best is not None:
        _, query, first, last = best
        return f"Google: '{query}' moved #{first} → #{last} this week."
    if entered is not None:
        last, query = entered
        return f"Google: '{query}' entered the results at #{last} this week."
    return None


_CHECK_NAMES: dict[str, str] | None = None


def _check_name(check_id: str) -> str | None:
    """Registry name for a check id; degrades to None (card shows the id)."""
    global _CHECK_NAMES
    if _CHECK_NAMES is None:
        try:
            from gm.audit.registry import load_registry

            _CHECK_NAMES = {
                cid: c.get("name") for cid, c in load_registry().checks.items()
            }
        except Exception:
            _CHECK_NAMES = {}
    return _CHECK_NAMES.get(check_id)


def _resolved_finding_line(conn: psycopg.Connection, site_id: Any) -> str | None:
    """Most recent content delta with at least one resolved audit finding."""
    rows = conn.execute(
        """
        select cd.findings_diff
          from content_deltas cd
          join content_items ci on ci.id = cd.content_item_id
         where ci.site_id = %s
         order by cd.created_at desc, cd.id
         limit 20
        """,
        (site_id,),
    ).fetchall()
    for r in rows:
        diff = r["findings_diff"] if isinstance(r["findings_diff"], dict) else {}
        resolved = diff.get("resolved") or []
        if resolved:
            check_id = str(resolved[0])
            label = _check_name(check_id) or check_id
            extra = f" (+{len(resolved) - 1} more)" if len(resolved) > 1 else ""
            return f"Fixed: {label} — resolved in the latest audit{extra}."
    return None


def _receipt_delta_line(conn: psycopg.Connection, site_id: Any) -> str | None:
    """Score movement from the latest monthly Delta Receipt (site_deltas)."""
    row = conn.execute(
        "select payload from site_deltas where site_id = %s"
        " order by created_at desc limit 1",
        (site_id,),
    ).fetchone()
    if row is None:
        return None
    payload = row["payload"] if isinstance(row["payload"], dict) else {}
    audits = payload.get("audits") if isinstance(payload.get("audits"), dict) else {}
    movement = audits.get("movement") if isinstance(audits.get("movement"), dict) else {}
    change = movement.get("change")
    if not isinstance(change, (int, float)) or isinstance(change, bool):
        return None
    first = (movement.get("first") or {}).get("score")
    last = (movement.get("last") or {}).get("score")
    span = ""
    if isinstance(first, (int, float)) and isinstance(last, (int, float)):
        span = f" ({first:g} → {last:g})"
    period = payload.get("period") or "the last period"
    if change > 0:
        return f"Site score up {change:g}{span} in {period}."
    if change < 0:
        return f"Site score down {abs(change):g}{span} in {period}."
    return f"Site score held steady{span} through {period}."


def _delta_line(
    conn: psycopg.Connection, site_id: Any, *, since: dt.date, until: dt.date
) -> str:
    line = _rank_delta_line(conn, site_id, since=since, until=until)
    if line is None:
        line = _resolved_finding_line(conn, site_id)
    if line is None:
        line = _receipt_delta_line(conn, site_id)
    if line is None:
        line = "No movement data yet — the next audit or rank check will fill this in."
    return line


def _next_action_line(conn: psycopg.Connection, site_id: Any) -> str:
    rows = conn.execute(
        """
        select kind, target, at_stake from queue_items
         where site_id = %s and status = 'open'
           and (snooze_until is null or snooze_until <= now())
         order by last_seen desc, id
         limit 50
        """,
        (site_id,),
    ).fetchall()
    if not rows:
        return "Next: queue is clear — no open opportunities right now."

    def est_gain(row: dict) -> float | None:
        at_stake = row["at_stake"] if isinstance(row["at_stake"], dict) else {}
        v = at_stake.get("est_clicks_gain")
        try:
            return float(v) if v is not None and not isinstance(v, bool) else None
        except (TypeError, ValueError):
            return None

    # Sort in Python: at_stake is free-form jsonb and a bad value must not
    # blow up the SQL cast; items with a known gain outrank those without.
    best = max(rows, key=lambda r: (est_gain(r) is not None, est_gain(r) or 0.0))
    target = best["target"] if isinstance(best["target"], dict) else {}
    name = target.get("query") or target.get("page") or ""
    label = _KIND_LABELS.get(best["kind"], str(best["kind"]).replace("_", " "))
    line = f"Next: {label}"
    if name:
        line += f" — '{name}'"
    gain = est_gain(best)
    if gain is not None and gain > 0:
        line += f" (~{gain:g} clicks/mo at stake)"
    return line


def build_card_text(conn: psycopg.Connection, site_id: Any, *, week_start: dt.date) -> str:
    """WhatsApp-ready plain-text trend card: 4 lines, <= 1024 chars, honest
    empty states on every line. Pure function of rows (plus now() only inside
    the queue snooze filter)."""
    stats = week_stats(conn, site_id, week_start=week_start)
    ws = stats["week_start"]
    lines = [
        _headline(stats),
        _delta_line(conn, site_id, since=ws, until=ws + dt.timedelta(days=6)),
        _next_action_line(conn, site_id),
        FOOTER,
    ]
    text = "\n".join(_clip(line, _LINE_LIMIT) for line in lines)
    # By construction 4*250+3 <= 1003; keep a hard guard anyway.
    return text if len(text) <= MAX_CARD_CHARS else _clip(text, MAX_CARD_CHARS)


# ---------------------------------------------------------------------------
# job handler
# ---------------------------------------------------------------------------

def _build_waba_client() -> Any:
    """Construct WabaClient from env; clear failures per contract.

    gm.delivery.whatsapp is a concurrent build — import lazily so this module
    (and card building/tests) never depend on its presence.
    """
    missing = [
        name
        for name in (WABA_TOKEN_ENV, WABA_PHONE_NUMBER_ID_ENV)
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(
            "cannot send lead card: missing env " + ", ".join(missing)
        )
    try:
        whatsapp = _import_module("gm.delivery.whatsapp")
    except ImportError as exc:
        raise RuntimeError(
            "cannot send lead card: gm.delivery.whatsapp is not available"
        ) from exc
    return whatsapp.WabaClient()


def handle_send_lead_card(ctx: jobs.JobContext, *, waba_client: Any = None) -> None:
    """Job 'send_lead_card' (weekly schedule): build the card and send it.

    Recipient comes from the site's connections kind='whatsapp' row
    (meta.recipient_wa_id); credentials are NOT stored — the token lives in
    env only. payload.week_start (ISO date) pins the reported week for
    deterministic retries; default is the current Monday-start week. The send
    is recorded as a zero-cost cost_event (provider='waba') for the audit
    trail; the recipient id is deliberately NOT copied into units.
    """
    site_id = ctx.job.site_id
    if site_id is None:
        raise ValueError("send_lead_card requires job.site_id")
    row = ctx.conn.execute(
        "select meta from connections where site_id = %s and kind = 'whatsapp'",
        (site_id,),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"no whatsapp connection for site {site_id} — run gm wa-connect first"
        )
    meta = row["meta"] if isinstance(row["meta"], dict) else {}
    recipient = meta.get("recipient_wa_id")
    if not recipient:
        raise ValueError(
            "whatsapp connection meta has no recipient_wa_id — re-run gm wa-connect"
        )

    raw_ws = ctx.job.payload.get("week_start")
    if raw_ws:
        try:
            week_start = dt.date.fromisoformat(raw_ws)
        except ValueError as exc:
            raise ValueError(
                f"invalid week_start {raw_ws!r} — expected an ISO date (YYYY-MM-DD)"
            ) from exc
    else:
        week_start = week_start_for(dt.datetime.now(dt.UTC).date())

    text = build_card_text(ctx.conn, site_id, week_start=week_start)
    client = waba_client if waba_client is not None else _build_waba_client()
    client.send_text(str(recipient), text)
    costs.record_cost(
        ctx.conn,
        provider="waba",
        purpose="lead_card",
        cost_cents=0.0,
        org_id=ctx.job.org_id,
        job_id=ctx.job.id,
        units={
            "messages": 1,
            "chars": len(text),
            "week_start": week_start_for(week_start).isoformat(),
        },
    )
