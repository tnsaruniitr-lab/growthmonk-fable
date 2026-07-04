"""Refusal log — the agency-pitch tripwire ledger (roadmap Phase D.7).

Every pitch that ends in a "no" gets a row: who, through which channel, and why
(one of the migration-012 check-list reasons). The point is the DIY share — the
roadmap's early-death tripwire fires when more than half the refusals over the
window say "we'll do it ourselves". That share is honest by construction:
`refusal_stats` returns diy_share=None when no refusals are logged, so an empty
ledger can never masquerade as "0% DIY".
"""

from __future__ import annotations

import datetime as dt

import psycopg

# Mirrors the check constraint in ops/migrations/012_phase_d4_refusals.sql.
REASONS: tuple[str, ...] = ("diy", "price", "timing", "trust", "other")


def add_refusal(
    conn: psycopg.Connection,
    *,
    org_id,
    prospect: str,
    reason: str,
    source: str = "agency_pitch",
    notes: str | None = None,
    refused_at: dt.date | None = None,
) -> str:
    """Insert one refusal row; returns its id.

    `reason` is validated against the migration-012 check list BEFORE the
    insert, so callers get a typed ValueError instead of a database
    CheckViolation. `refused_at` defaults to current_date (the column default).
    """
    if reason not in REASONS:
        raise ValueError(f"reason must be one of {'/'.join(REASONS)}, got {reason!r}")
    row = conn.execute(
        "insert into refusals (org_id, prospect, source, reason, notes, refused_at)"
        " values (%s, %s, %s, %s, %s, coalesce(%s, current_date)) returning id",
        (org_id, prospect, source, reason, notes, refused_at),
    ).fetchone()
    return str(row["id"])


def list_refusals(conn: psycopg.Connection, *, org_id, days: int = 180) -> list[dict]:
    """Refusals within the window, newest first.

    Window is inclusive: refused_at >= current_date - days (a refusal exactly
    `days` old is still in; one day older is out).
    """
    return conn.execute(
        "select id, org_id, prospect, source, reason, notes, refused_at, created_at"
        "  from refusals"
        " where org_id = %s and refused_at >= current_date - %s"
        " order by refused_at desc, created_at desc",
        (org_id, days),
    ).fetchall()


def refusal_stats(conn: psycopg.Connection, *, org_id, days: int = 180) -> dict:
    """{"total", "by_reason": {every check-list reason: n}, "diy_share": float|None}.

    diy_share is None when total == 0 — the tripwire must never read
    "no refusals logged" as "0% DIY". by_reason always carries every reason
    from the check list (true zeros: the reason was pitchable and got 0 hits).
    """
    rows = conn.execute(
        "select reason, count(*) as n from refusals"
        " where org_id = %s and refused_at >= current_date - %s"
        " group by reason",
        (org_id, days),
    ).fetchall()
    by_reason = {reason: 0 for reason in REASONS}
    for r in rows:
        by_reason[r["reason"]] = r["n"]
    total = sum(by_reason.values())
    return {
        "total": total,
        "by_reason": by_reason,
        "diy_share": (by_reason["diy"] / total) if total else None,
    }
