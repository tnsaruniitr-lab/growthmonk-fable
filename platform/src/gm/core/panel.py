"""Panel management: orgs, sites, immutable versioned prompts, frozen run panels."""

from __future__ import annotations

import hashlib
import json

import psycopg

from gm.intel.engines.base import normalize_host


def prompt_hash(prompt: str) -> str:
    return hashlib.md5(prompt.strip().lower().encode()).hexdigest()


def create_org(conn: psycopg.Connection, name: str) -> str:
    row = conn.execute("insert into orgs (name) values (%s) returning id", (name,)).fetchone()
    return str(row["id"])


def get_default_org(conn: psycopg.Connection) -> dict:
    rows = conn.execute("select id, name from orgs order by created_at").fetchall()
    if not rows:
        raise RuntimeError("No org exists yet — run: gm org create <name>")
    if len(rows) > 1:
        raise RuntimeError("Multiple orgs exist — pass --org explicitly")
    return rows[0]


def add_site(
    conn: psycopg.Connection,
    org_id: str,
    domain: str,
    *,
    is_control: bool = False,
    brand_terms: list[str] | None = None,
    notes: str | None = None,
) -> str:
    row = conn.execute(
        "insert into sites (org_id, domain_norm, is_control, brand_terms, notes)"
        " values (%s, %s, %s, %s, %s)"
        " on conflict (org_id, domain_norm) do update set is_control = excluded.is_control"
        " returning id",
        (org_id, normalize_host(domain), is_control, brand_terms or [], notes),
    ).fetchone()
    return str(row["id"])


def get_site(conn: psycopg.Connection, org_id: str, domain: str) -> dict:
    row = conn.execute(
        "select * from sites where org_id=%s and domain_norm=%s",
        (org_id, normalize_host(domain)),
    ).fetchone()
    if not row:
        raise RuntimeError(f"Unknown site: {domain} — run: gm site add {domain}")
    return row


def add_prompt(
    conn: psycopg.Connection, org_id: str, site_id: str, prompt: str, engines: list[str]
) -> str:
    h = prompt_hash(prompt)
    existing = conn.execute(
        "select id from tracked_prompts where site_id=%s and prompt_hash=%s and active",
        (site_id, h),
    ).fetchone()
    if existing:
        return str(existing["id"])
    row = conn.execute(
        "insert into tracked_prompts (org_id, site_id, prompt, prompt_hash, engines)"
        " values (%s, %s, %s, %s, %s) returning id",
        (org_id, site_id, prompt, h, engines),
    ).fetchone()
    return str(row["id"])


def supersede_prompt(
    conn: psycopg.Connection, org_id: str, old_prompt_id: str, new_prompt: str, engines: list[str]
) -> str:
    old = conn.execute(
        "update tracked_prompts set active=false where id=%s returning site_id", (old_prompt_id,)
    ).fetchone()
    if not old:
        raise RuntimeError(f"Unknown prompt id: {old_prompt_id}")
    row = conn.execute(
        "insert into tracked_prompts (org_id, site_id, prompt, prompt_hash, engines, supersedes_id)"
        " values (%s, %s, %s, %s, %s, %s) returning id",
        (org_id, old["site_id"], new_prompt, prompt_hash(new_prompt), engines, old_prompt_id),
    ).fetchone()
    return str(row["id"])


def active_prompts(conn: psycopg.Connection, site_id: str) -> list[dict]:
    return conn.execute(
        "select id, prompt, engines from tracked_prompts where site_id=%s and active"
        " order by created_at",
        (site_id,),
    ).fetchall()


def freeze_panel(conn: psycopg.Connection, site_id: str) -> list[dict]:
    return [
        {"prompt_id": str(p["id"]), "prompt": p["prompt"], "engines": list(p["engines"])}
        for p in active_prompts(conn, site_id)
    ]


def panel_hash(panel: list[dict]) -> str:
    return hashlib.sha256(json.dumps(panel, sort_keys=True).encode()).hexdigest()[:16]
