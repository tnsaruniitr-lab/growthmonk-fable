"""Connection + migration runner + org scoping.

Discipline (ADR-14): every unit of tenant work runs inside a transaction that has
executed SET LOCAL app.org_id first. `set_org` is the only sanctioned way to do that.
"""

from __future__ import annotations

import uuid

import psycopg
from psycopg.rows import dict_row

from gm import config


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(config.database_url(), autocommit=autocommit, row_factory=dict_row)


def set_org(conn: psycopg.Connection, org_id: str | uuid.UUID | None) -> None:
    """SET LOCAL app.org_id inside the current transaction. Call after BEGIN."""
    if org_id is None:
        return
    conn.execute("select set_config('app.org_id', %s, true)", (str(org_id),))


def run_migrations() -> list[str]:
    """Apply ops/migrations/*.sql in filename order; track in schema_migrations."""
    mdir = config.migrations_dir()
    applied: list[str] = []
    with connect(autocommit=True) as conn:
        conn.execute(
            "create table if not exists schema_migrations ("
            " version text primary key, applied_at timestamptz not null default now())"
        )
        done = {
            r["version"] for r in conn.execute("select version from schema_migrations").fetchall()
        }
        for path in sorted(mdir.glob("*.sql")):
            if path.name in done:
                continue
            with connect() as tx:
                tx.execute(path.read_text())
                tx.execute("insert into schema_migrations (version) values (%s)", (path.name,))
                tx.commit()
            applied.append(path.name)
    return applied
