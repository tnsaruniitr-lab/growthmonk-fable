"""Post-publish verification (Phase C wave-3): did the post actually land?

Three probes against the published URL, each persisted as its own
verify_events row (the table's kind enum: bev | schema | inspection):

1. BEV re-probe — gm.audit.bev with the real fetcher-factory pattern
   (production: ``lambda ua: make_fetcher(user_agent=ua)``; tests inject fake
   fetchers). Pass = a gradeable content classification AND every AI-bot UA
   got a 2xx.
2. Schema presence — gm.audit.inspectors.schema_markup.inspect_schema on the
   SERVED HTML (kses may have stripped the JSON-LD the draft carried; only the
   served page is the truth).
3. GSC URL inspection — advisory, only when a gsc connection exists for the
   site; absence (or a locked vault / API error) is tolerated with a note,
   never a failure.

Verdict transitions happen on the LATE attempt only (T+72h): pass ->
content_items.status='verified', fail -> 'verify_failed' with the honest
probe results already on record. The early attempt (T+15m) records evidence
but draws no verdict — indexing signals are meaningless minutes after publish.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
import psycopg
from psycopg.types.json import Jsonb

from gm.audit.bev import bots_eye_view
from gm.audit.fetch import DEFAULT_USER_AGENT, make_fetcher
from gm.audit.inspectors.schema_markup import inspect_schema
from gm.audit.safety import UnsafeURL
from gm.connections import vault
from gm.connections.gsc import GscClient, GscError

log = logging.getLogger(__name__)

ATTEMPTS = ("early", "late")
BOT_PROBES = ("googlebot", "gptbot", "perplexitybot", "claudebot")

# BEV classifications that mean "bots can read the content".
CONTENT_OK_CLASSES = frozenset({"fully_accessible", "partial_ssr"})


def _record_event(conn, org_id, content_item_id, kind: str, result: dict) -> str:
    row = conn.execute(
        "insert into verify_events (org_id, content_item_id, kind, result)"
        " values (%s, %s, %s, %s) returning id",
        (org_id, content_item_id, kind, Jsonb(result)),
    ).fetchone()
    return str(row["id"])


def _default_gsc_client(connection_row: dict) -> GscClient:
    creds = connection_row.get("credentials") or {}
    prop = (connection_row.get("meta") or {}).get("property") or ""
    return GscClient(service_account_info=creds, property_url=prop)


def verify_publish(
    conn: psycopg.Connection,
    *,
    content_item_id,
    attempt: str,
    fetcher_factory: Callable | None = None,
    gsc_client_factory: Callable[[dict], object] | None = None,
) -> dict:
    """Run the three probes for the latest publish event of `content_item_id`.

    Returns the honest result dict; on attempt='late' also transitions
    content_items.status to 'verified' or 'verify_failed'.
    """
    if attempt not in ATTEMPTS:
        raise ValueError(f"attempt must be one of {ATTEMPTS}, got {attempt!r}")
    item = conn.execute(
        "select * from content_items where id = %s", (content_item_id,)
    ).fetchone()
    if item is None:
        raise LookupError(f"content_item {content_item_id} not found")
    org_id, site_id = item["org_id"], item["site_id"]
    event = conn.execute(
        "select * from publish_events where content_item_id = %s and url is not null"
        " order by published_at desc limit 1",
        (content_item_id,),
    ).fetchone()
    if event is None:
        raise LookupError(f"content_item {content_item_id} has no publish event with a url")
    url = event["url"]
    factory = fetcher_factory or (lambda ua: make_fetcher(user_agent=ua))
    notes: list[str] = []

    # Probe 1: BEV — do all UAs (browser + AI bots) read the content?
    bev = bots_eye_view(url, factory)
    bot_status = {name: bev.per_ua.get(name, {}).get("status") for name in BOT_PROBES}
    bots_2xx = all(isinstance(s, int) and 200 <= s < 300 for s in bot_status.values())
    bev_ok = bev.classification in CONTENT_OK_CLASSES and bots_2xx
    _record_event(conn, org_id, content_item_id, "bev", {
        "attempt": attempt,
        "url": url,
        "classification": bev.classification,
        "bot_status": bot_status,
        "cloaking_suspected": bev.cloaking_suspected,
        "ok": bev_ok,
        "notes": bev.notes,
    })

    # Probe 2: schema presence in the SERVED HTML.
    schema_present = False
    schema_result: dict = {"attempt": attempt, "url": url}
    try:
        page = factory(DEFAULT_USER_AGENT)(url)
    except (httpx.HTTPError, UnsafeURL) as exc:
        schema_result["note"] = f"schema probe fetch failed: {type(exc).__name__}: {exc}"
    else:
        inspected = inspect_schema(page.text, url)
        summary = inspected.get("schema_summary") or {}
        schema_present = int(summary.get("total_entities") or 0) > 0
        schema_result["schema_summary"] = summary
    schema_result["present"] = schema_present
    _record_event(conn, org_id, content_item_id, "schema", schema_result)

    # Probe 3: GSC URL inspection — advisory; tolerate absence.
    inspection: dict | None = None
    try:
        row = vault.load_connection(conn, site_id, "gsc")
    except (LookupError, vault.VaultLocked) as exc:
        notes.append(f"gsc inspection skipped: {exc}")
    else:
        try:
            gsc = (gsc_client_factory or _default_gsc_client)(row)
            inspection = gsc.inspect_url(url)
            _record_event(conn, org_id, content_item_id, "inspection",
                          {"attempt": attempt, "url": url, **inspection})
        except (GscError, httpx.HTTPError) as exc:
            notes.append(f"gsc inspection failed (advisory, not a verify failure): {exc}")

    passed = bev_ok and schema_present
    if not bev_ok:
        notes.append(
            f"bev: classification={bev.classification}, bot_status={bot_status}"
        )
    if not schema_present:
        notes.append("schema: no JSON-LD entities in the served HTML")

    if attempt == "late":
        new_status = "verified" if passed else "verify_failed"
        conn.execute(
            "update content_items set status = %s, updated_at = now() where id = %s",
            (new_status, content_item_id),
        )
        log.info("content_item %s: late verify -> %s", content_item_id, new_status)

    return {
        "url": url,
        "attempt": attempt,
        "passed": passed,
        "bev_ok": bev_ok,
        "bev_classification": bev.classification,
        "bot_status": bot_status,
        "schema_present": schema_present,
        "inspection": inspection,
        "notes": notes,
    }


def handle_verify_publish(ctx) -> None:
    """Job handler for type 'verify_publish': payload {content_item_id, attempt}."""
    payload = ctx.job.payload or {}
    content_item_id = payload.get("content_item_id")
    if not content_item_id:
        raise ValueError(f"job {ctx.job.id}: payload missing 'content_item_id'")
    verify_publish(
        ctx.conn,
        content_item_id=content_item_id,
        attempt=str(payload.get("attempt") or "early"),
    )
