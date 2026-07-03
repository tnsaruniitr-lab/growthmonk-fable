"""Cost events + quota ledgers. Every paid call records a cost_event (principle 8)."""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb


def record_cost(
    conn: psycopg.Connection,
    *,
    provider: str,
    purpose: str,
    cost_cents: float,
    org_id=None,
    job_id: int | None = None,
    units: dict | None = None,
) -> None:
    conn.execute(
        "insert into cost_events (org_id, job_id, provider, purpose, units, cost_cents)"
        " values (%s, %s, %s, %s, %s, %s)",
        (org_id, job_id, provider, purpose, Jsonb(units or {}), cost_cents),
    )


def bump_quota(conn: psycopg.Connection, port: str, scope: str, n: int = 1) -> int:
    row = conn.execute(
        "insert into quota_ledgers (port, scope, date, used) values (%s, %s, current_date, %s)"
        " on conflict (port, scope, date) do update set used = quota_ledgers.used + excluded.used"
        " returning used",
        (port, scope, n),
    ).fetchone()
    return row["used"]


def quota_used(conn: psycopg.Connection, port: str, scope: str) -> int:
    row = conn.execute(
        "select used from quota_ledgers where port=%s and scope=%s and date=current_date",
        (port, scope),
    ).fetchone()
    return row["used"] if row else 0
