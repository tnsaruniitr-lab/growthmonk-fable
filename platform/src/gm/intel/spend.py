"""DataForSEO spend rail — rollup, live balance, monthly budget guard (Phase D4, WP-I).

Three read-only views plus one guard:
- `spend_rollup` aggregates cost_events over a rolling window for the operator
  console / `gm spend` (every provider, not just DataForSEO).
- `dataforseo_balance` reads the live account balance via the FREE
  /v3/appendix/user_data endpoint (no cost_event; never raises — an unreachable
  balance is an honest None + note, per the empty-state law).
- `budget_state` compares this CALENDAR month's DataForSEO cost_events against
  the optional GM_DFS_MONTHLY_BUDGET_CENTS cap and projects the month's burn.
- `require_dfs_budget` raises BudgetExceeded(retryable=False) when the cap is
  reached — called by paid call sites (serp.get_snapshot / get_volumes purchase
  paths, labs.keyword_gap) BEFORE spending, never after: a refusal costs $0.

This module deliberately imports nothing from gm.intel.serp / gm.intel.labs so
their purchase paths can lazy-import it without a cycle. The Basic-auth env
names (DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD) are serp.py's, never renamed.
"""

from __future__ import annotations

import base64
import calendar
import datetime as dt
import logging
import os

import httpx
import psycopg

log = logging.getLogger(__name__)

BALANCE_URL = "https://api.dataforseo.com/v3/appendix/user_data"
BUDGET_ENV = "GM_DFS_MONTHLY_BUDGET_CENTS"
DFS_PROVIDER = "dataforseo"
DEFAULT_TIMEOUT_SECONDS = 30.0


class BudgetExceeded(Exception):
    """Typed refusal raised BEFORE any paid DataForSEO call — a refusal costs $0.

    retryable=False: the job queue's max_attempts still bounds retries (each one
    is a cheap no-spend refusal), so a guarded job goes dead with this message in
    jobs.last_error instead of looping forever or silently skipping.
    """

    retryable = False

    def __init__(self, *, cap_cents: int, spent_cents: float):
        self.cap_cents = cap_cents
        self.spent_cents = spent_cents
        super().__init__(
            f"dataforseo monthly budget exceeded: {spent_cents:g} of {cap_cents} cents spent"
            f" this calendar month; refusing paid call before spending"
            f" (raise or unset {BUDGET_ENV} to resume)"
        )


# --- spend rollup -------------------------------------------------------------------------


def spend_rollup(conn: psycopg.Connection, *, days: int = 30) -> dict:
    """Aggregate cost_events over the last `days` days (all providers).

    {"window_days", "total_cents", "by_provider": [{"provider","cost_cents","events"}],
     "by_purpose": [{"provider","purpose","cost_cents","events"}] (both cost desc),
     "by_day": [{"date","provider","cost_cents"}] chronological,
     "last_event": {"created_at","provider","purpose","cost_cents","units"}|None}.

    An empty window is an honest true-zero (total_cents 0.0 = nothing spent);
    last_event is None when the window holds no events. All figures are
    window-scoped, including last_event.
    """
    window = "created_at > now() - make_interval(days => %s)"
    by_provider = [
        dict(r)
        for r in conn.execute(
            "select provider, sum(cost_cents)::float8 as cost_cents, count(*)::int as events"
            f" from cost_events where {window}"
            " group by provider order by cost_cents desc, provider",
            (days,),
        ).fetchall()
    ]
    by_purpose = [
        dict(r)
        for r in conn.execute(
            "select provider, purpose, sum(cost_cents)::float8 as cost_cents,"
            " count(*)::int as events"
            f" from cost_events where {window}"
            " group by provider, purpose order by cost_cents desc, provider, purpose",
            (days,),
        ).fetchall()
    ]
    by_day = [
        dict(r)
        for r in conn.execute(
            "select created_at::date as date, provider, sum(cost_cents)::float8 as cost_cents"
            f" from cost_events where {window}"
            " group by 1, 2 order by date, provider",
            (days,),
        ).fetchall()
    ]
    last = conn.execute(
        "select created_at, provider, purpose, cost_cents::float8 as cost_cents, units"
        f" from cost_events where {window}"
        " order by created_at desc, id desc limit 1",
        (days,),
    ).fetchone()
    return {
        "window_days": days,
        "total_cents": sum(r["cost_cents"] for r in by_provider),
        "by_provider": by_provider,
        "by_purpose": by_purpose,
        "by_day": by_day,
        "last_event": dict(last) if last is not None else None,
    }


# --- live balance (free endpoint, never raises) ---------------------------------------------


def _parse_balance(data: object) -> float | None:
    """tasks[0].result[0].money.balance from the user_data envelope; None on any bad level."""
    if not isinstance(data, dict) or data.get("status_code") != 20000:
        return None
    tasks = data.get("tasks")
    task = tasks[0] if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict) else None
    if task is None or task.get("status_code") != 20000:
        return None
    result = task.get("result")
    entry = (
        result[0] if isinstance(result, list) and result and isinstance(result[0], dict) else None
    )
    if entry is None:
        return None
    money = entry.get("money")
    balance = money.get("balance") if isinstance(money, dict) else None
    if isinstance(balance, int | float) and not isinstance(balance, bool):
        return float(balance)
    return None


def dataforseo_balance(client: httpx.Client | None = None) -> dict:
    """Live DataForSEO account balance (dollars) via GET /v3/appendix/user_data.

    The endpoint is free: no cost_event is recorded. Returns {"balance":
    float|None, "note": str|None} — balance None plus an honest note on missing
    env, transport failure, or a bad envelope. NEVER raises into callers; a
    true-zero balance stays 0.0, never None.
    """
    login = os.environ.get("DATAFORSEO_LOGIN", "")
    password = os.environ.get("DATAFORSEO_PASSWORD", "")
    if not login or not password:
        return {"balance": None, "note": "DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set"}
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    owns_client = client is None
    client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        try:
            resp = client.get(BALANCE_URL, headers={"Authorization": f"Basic {token}"})
        except httpx.HTTPError as exc:
            return {"balance": None, "note": f"dataforseo balance unreachable: {exc}"}
        if resp.status_code != 200:
            return {"balance": None, "note": f"dataforseo balance: HTTP {resp.status_code}"}
        try:
            data = resp.json()
        except ValueError:
            return {"balance": None, "note": "dataforseo balance: non-JSON response body"}
        balance = _parse_balance(data)
        if balance is None:
            return {"balance": None, "note": "dataforseo balance: unexpected response envelope"}
        return {"balance": balance, "note": None}
    finally:
        if owns_client:
            client.close()


# --- monthly budget -------------------------------------------------------------------------


def _cap_cents() -> tuple[int | None, str | None]:
    """(cap, note) from GM_DFS_MONTHLY_BUDGET_CENTS; unset/blank/non-int -> no cap + note."""
    raw = (os.environ.get(BUDGET_ENV) or "").strip()
    if not raw:
        return None, "no cap configured"
    try:
        return int(raw), None
    except ValueError:
        return None, f"{BUDGET_ENV} is not an integer ({raw!r}); no cap applied"


def budget_state(conn: psycopg.Connection, *, now: dt.datetime | None = None) -> dict:
    """DataForSEO spend vs the optional monthly cap, over the calendar month of `now`.

    {"cap_cents": int|None, "spent_cents": float (provider='dataforseo' cost_events
    this calendar month — 0.0 is an honest true-zero), "projected_month_cents":
    float|None (spent / days_elapsed * days_in_month; None without spend — a
    projection from nothing would be invented), "exceeded": bool (False when no
    cap; True at or over the cap — the cap is a ceiling), "note": str|None}.
    """
    now = now if now is not None else dt.datetime.now(dt.UTC)
    cap_cents, note = _cap_cents()
    spent = conn.execute(
        "select coalesce(sum(cost_cents), 0)::float8 as spent from cost_events"
        " where provider = %s"
        " and created_at >= date_trunc('month', %s::timestamptz)"
        " and created_at < date_trunc('month', %s::timestamptz) + interval '1 month'",
        (DFS_PROVIDER, now, now),
    ).fetchone()["spent"]
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    projected = spent / now.day * days_in_month if spent > 0 else None
    return {
        "cap_cents": cap_cents,
        "spent_cents": spent,
        "projected_month_cents": projected,
        "exceeded": cap_cents is not None and spent >= cap_cents,
        "note": note,
    }


def require_dfs_budget(conn: psycopg.Connection) -> None:
    """Raise BudgetExceeded when the monthly DataForSEO cap is reached.

    Call sites check BEFORE spending, never after — a refusal costs $0. No cap
    configured means no refusal (absence is an honest state, not an error).
    """
    state = budget_state(conn)
    if state["exceeded"]:
        raise BudgetExceeded(cap_cents=state["cap_cents"], spent_cents=state["spent_cents"])
