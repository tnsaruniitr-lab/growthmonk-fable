"""Share tokens for public audit reports (docs/phase-b-wave2-contracts.md).

The raw token is returned exactly once at creation; only its sha256 hex ever
touches the database (report_shares.token_hash). `resolve_share` is the ONE
query in the codebase that runs WITHOUT org context — /r/{token} is an
unauthenticated surface, so it selects by token_hash alone and hands org_id
back to the caller, which then sets org context for every subsequent query.
"""

from __future__ import annotations

import hashlib
import secrets

import psycopg

# token_urlsafe(32) is 43 chars; anything much longer is garbage — refuse to
# hash multi-megabyte inputs from the unauthenticated surface.
_MAX_TOKEN_LEN = 128


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_share(
    conn: psycopg.Connection, org_id: str, audit_id: str, ttl_days: int = 60
) -> str:
    """Create a share row for an audit and return the RAW token (never stored)."""
    raw = secrets.token_urlsafe(32)
    conn.execute(
        "insert into report_shares (org_id, audit_id, token_hash, expires_at)"
        " values (%s, %s, %s, now() + make_interval(days => %s))",
        (org_id, audit_id, _token_hash(raw), ttl_days),
    )
    return raw


def resolve_share(conn: psycopg.Connection, raw_token: str) -> dict | None:
    """Resolve a raw token to {"audit_id", "org_id"}, or None.

    None on miss, expiry, or revocation alike — callers must not be able to
    distinguish (no oracle). The comparison happens on sha256 digests via the
    unique index: a b-tree lookup over uniformly distributed digests leaks no
    usable timing signal about the raw token, which is the constant-time
    property that matters here.

    Runs without org context by design; the caller sets org context from the
    returned org_id before touching any other tenant table.
    """
    if not raw_token or len(raw_token) > _MAX_TOKEN_LEN:
        return None
    row = conn.execute(
        "select audit_id, org_id from report_shares"
        " where token_hash = %s and not revoked and expires_at > now()",
        (_token_hash(raw_token),),
    ).fetchone()
    if row is None:
        return None
    return {"audit_id": str(row["audit_id"]), "org_id": str(row["org_id"])}
