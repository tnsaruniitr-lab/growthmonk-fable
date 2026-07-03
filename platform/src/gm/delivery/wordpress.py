"""WordPress delivery port — Application Passwords, least privilege (Phase C wave-3).

Security posture (per the wave-3 security review):
- https only: the client REFUSES http:// base URLs — Application Passwords are
  Basic auth, and sending them in cleartext is a credential leak, not a
  degraded mode.
- Least privilege: preflight warns when the connected account holds the
  `administrator` role (an Editor/Author account is all publishing needs), and
  proves capabilities empirically by creating and deleting a PRIVATE test
  draft rather than trusting the role name.
- kses honesty: WordPress strips `<script>` blocks (incl. JSON-LD) for users
  without `unfiltered_html`. Preflight round-trips a JSON-LD block through a
  test draft and compares; publish_draft repeats the comparison and notes the
  caveat in its result instead of silently shipping schema-less posts.

All HTTP goes through an injectable httpx.Client (tests: MockTransport). The
retry/backoff pattern is a local copy of the gm.connections.gsc one (429/5xx/
transport retries inside a total time budget); 401/403 are never retried and
are classified so "Application Passwords disabled" reads as itself, not as a
generic auth failure.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import random
import time
from urllib.parse import urlsplit

import httpx
import psycopg
from psycopg.types.json import Jsonb

from gm.audit.pipeline import canonicalize_url
from gm.connections import vault
from gm.infra.jobs import enqueue

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 3
JSONLD_MARKER = '<script type="application/ld+json">'
INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"

# Verify re-probes: quick sanity at T+15m, the honest verdict at T+72h.
VERIFY_DELAYS: dict[str, dt.timedelta] = {
    "early": dt.timedelta(minutes=15),
    "late": dt.timedelta(hours=72),
}

# Module-level indirection so tests can patch out real sleeping.
_sleep = time.sleep


class WpError(Exception):
    """WordPress REST failure. `retryable` mirrors the jobs-layer convention."""

    def __init__(self, message: str, retryable: bool = False,
                 status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.code = code


def _backoff_seconds(attempt: int, remaining: float) -> float:
    """Exponential backoff with jitter, capped by the remaining time budget."""
    base = min(8.0, 0.5 * (2**attempt))
    return max(0.0, min(base + random.uniform(0.0, base / 4), max(remaining, 0.0)))


def _jsonld_block(jsonld) -> str:
    """Render a JSON-LD payload (dict/list or pre-serialized string) as a script block."""
    body = jsonld if isinstance(jsonld, str) else json.dumps(jsonld, ensure_ascii=False)
    return f"{JSONLD_MARKER}{body}</script>"


def _content_raw(post: dict) -> str:
    """Best-available post content from a ?context=edit response (raw, else rendered)."""
    content = post.get("content")
    if isinstance(content, dict):
        return content.get("raw") or content.get("rendered") or ""
    return content if isinstance(content, str) else ""


def _auth_message(status: int, body: dict | None, text: str) -> tuple[str, str]:
    """Classify a 401/403 body. Returns (error_code, human message)."""
    code = str((body or {}).get("code") or "")
    message = str((body or {}).get("message") or text[:200])
    if "application_password" in code or "application password" in message.lower():
        return code, (
            f"wordpress: HTTP {status} — Application Passwords are disabled or unavailable "
            f"on this site ({code or 'no code'}): enable them under Users → Profile "
            f"(requires https) and generate a new application password"
        )
    return code, (
        f"wordpress: HTTP {status} authentication failed ({code or 'no code'}): {message} "
        f"— check the username and application password"
    )


class WpClient:
    """Minimal WP REST client for one site, authenticated with an Application Password."""

    def __init__(self, base_url: str, username: str, app_password: str,
                 client: httpx.Client | None = None):
        parts = urlsplit(base_url)
        if parts.scheme != "https":
            raise WpError(
                f"wordpress: refusing non-https base_url {base_url!r} — Application "
                f"Passwords are Basic auth and must never travel over http"
            )
        self.base_url = base_url.rstrip("/")
        self._api = f"{self.base_url}/wp-json/wp/v2"
        token = base64.b64encode(f"{username}:{app_password}".encode()).decode("ascii")
        self._headers = {"Authorization": f"Basic {token}"}
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)

    # -- transport ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        params: dict | None = None,
        max_retries: int = MAX_RETRIES,
        total_budget_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict:
        """One REST call -> parsed JSON dict (retry pattern: local copy of the gsc one)."""
        url = f"{self._api}{path}"
        deadline = time.monotonic() + total_budget_seconds
        attempt = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WpError(
                    f"wordpress: {total_budget_seconds:.0f}s total budget exhausted",
                    retryable=True,
                )
            try:
                resp = self._client.request(
                    method, url, headers=self._headers, json=payload, params=params,
                    timeout=min(remaining, total_budget_seconds),
                )
            except httpx.HTTPError as exc:
                if attempt >= max_retries:
                    raise WpError(
                        f"wordpress: transport failure after {attempt + 1} attempts: {exc}",
                        retryable=True,
                    ) from exc
                attempt += 1
                _sleep(_backoff_seconds(attempt, deadline - time.monotonic()))
                continue
            if resp.status_code in (401, 403):
                body = None
                try:
                    body = resp.json()
                except ValueError:
                    pass
                code, message = _auth_message(resp.status_code, body, resp.text)
                raise WpError(message, retryable=False,
                              status_code=resp.status_code, code=code or None)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= max_retries:
                    raise WpError(
                        f"wordpress: HTTP {resp.status_code} after {attempt + 1} attempts",
                        retryable=True, status_code=resp.status_code,
                    )
                attempt += 1
                _sleep(_backoff_seconds(attempt, deadline - time.monotonic()))
                continue
            if resp.status_code >= 400:
                raise WpError(
                    f"wordpress: HTTP {resp.status_code}: {resp.text[:500]}",
                    retryable=False, status_code=resp.status_code,
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise WpError("wordpress: non-JSON response body", retryable=False) from exc
            if not isinstance(data, dict):
                raise WpError(
                    f"wordpress: unexpected JSON payload type {type(data).__name__}",
                    retryable=False,
                )
            return data

    # -- API surface ----------------------------------------------------------------

    def me(self) -> dict:
        """The authenticated user incl. roles + capabilities (?context=edit)."""
        return self._request("GET", "/users/me", params={"context": "edit"})

    def preflight(self) -> dict:
        """Prove the connection works with least privilege before storing it.

        Steps: auth check (roles/capabilities), administrator-role warning,
        create a PRIVATE test draft carrying a JSON-LD block, compare the
        round-tripped content (kses detection), delete the test draft.
        Returns {ok, role, warnings, errors}.
        """
        report: dict = {"ok": False, "role": None, "warnings": [], "errors": []}
        try:
            me = self.me()
        except WpError as exc:
            report["errors"].append(str(exc))
            return report
        roles = [r for r in (me.get("roles") or []) if isinstance(r, str)]
        report["role"] = roles[0] if roles else None
        if "administrator" in roles:
            report["warnings"].append(
                "account has the administrator role — publishing only needs Editor/Author; "
                "use a least-privilege account so a leaked application password cannot "
                "take over the site"
            )
        caps = me.get("capabilities") or {}
        if caps and not caps.get("edit_posts"):
            report["errors"].append(
                f"account {me.get('name')!r} lacks the edit_posts capability — "
                f"connect an Author/Editor account"
            )
            return report

        test_jsonld = {"@context": "https://schema.org", "@type": "WebPage",
                       "name": "GrowthMonk preflight"}
        body = (
            _jsonld_block(test_jsonld)
            + "\n<p>GrowthMonk connection preflight — safe to delete.</p>"
        )
        try:
            created = self._request(
                "POST", "/posts",
                payload={"title": "GrowthMonk preflight (auto-deleted)",
                         "status": "private", "content": body},
                params={"context": "edit"},
            )
        except WpError as exc:
            report["errors"].append(f"could not create a private test draft: {exc}")
            return report
        if JSONLD_MARKER not in _content_raw(created):
            report["warnings"].append(
                "kses stripped the JSON-LD <script> block on round-trip — published posts "
                "will lose schema markup; grant unfiltered_html or inject JSON-LD via an "
                "SEO/snippet plugin instead"
            )
        post_id = created.get("id")
        try:
            self._request("DELETE", f"/posts/{post_id}", params={"force": "true"})
        except WpError as exc:
            report["warnings"].append(
                f"test draft {post_id} was created but could not be deleted: {exc}"
            )
        report["ok"] = not report["errors"]
        return report

    def publish_draft(self, *, title: str, content_html: str, excerpt: str | None = None,
                      meta_jsonld=None) -> dict:
        """Create the post as a WP DRAFT (status=draft) — a human presses Publish.

        JSON-LD is prepended as a <script type="application/ld+json"> block; the
        response content is compared round-trip and a kses caveat is noted in the
        result when the block was stripped. Returns {id, link, status} (+ notes).
        """
        content = content_html
        if meta_jsonld is not None:
            content = f"{_jsonld_block(meta_jsonld)}\n{content_html}"
        payload: dict = {"title": title, "content": content, "status": "draft"}
        if excerpt:
            payload["excerpt"] = excerpt
        created = self._request("POST", "/posts", payload=payload, params={"context": "edit"})
        result: dict = {
            "id": created.get("id"),
            "link": created.get("link"),
            "status": created.get("status"),
        }
        if meta_jsonld is not None and JSONLD_MARKER not in _content_raw(created):
            result["jsonld_stripped"] = True
            result["note"] = (
                "kses stripped the JSON-LD <script> block — the draft has no schema "
                "markup; grant unfiltered_html or use an SEO/snippet plugin"
            )
        return result


# ---------------------------------------------------------------------------
# Connection + publish flow
# ---------------------------------------------------------------------------

def connect_wordpress(conn: psycopg.Connection, *, org_id, site_id, base_url: str,
                      username: str, app_password: str, wp: WpClient | None = None) -> dict:
    """Preflight, then store the connection in the vault (only when preflight passes).

    Returns the preflight report (+ stored/connection_id). `wp` is injectable
    for tests; production builds the client from the given credentials.
    """
    wp = wp or WpClient(base_url, username, app_password)
    report = wp.preflight()
    if report["ok"]:
        report["connection_id"] = vault.store_connection(
            conn, org_id=org_id, site_id=site_id, kind="wordpress",
            credentials={"base_url": base_url, "username": username,
                         "app_password": app_password},
            meta={"preflight": {"role": report["role"], "warnings": report["warnings"]}},
        )
        report["stored"] = True
    else:
        report["stored"] = False
    return report


def client_from_connection(conn: psycopg.Connection, site_id) -> WpClient:
    """Build a WpClient from the vault-stored wordpress connection for `site_id`."""
    row = vault.load_connection(conn, site_id, "wordpress")
    creds = row.get("credentials") or {}
    return WpClient(creds["base_url"], creds["username"], creds["app_password"])


def indexnow_ping(url: str, *, key: str | None = None,
                  client: httpx.Client | None = None) -> dict:
    """Fire-and-forget IndexNow ping. Never raises.

    Key comes from INDEXNOW_KEY when not passed; unset -> skipped with an
    honest note (the caller logs it, nothing fails).
    """
    key = key if key is not None else os.environ.get("INDEXNOW_KEY")
    if not key:
        return {"sent": False, "note": "INDEXNOW_KEY unset — IndexNow ping skipped"}
    host = urlsplit(url).hostname or ""
    own_client = client is None
    http = client or httpx.Client(timeout=10.0)
    try:
        resp = http.post(INDEXNOW_ENDPOINT, json={"host": host, "key": key, "urlList": [url]})
        return {"sent": True, "status": resp.status_code}
    except httpx.HTTPError as exc:
        return {"sent": False, "note": f"IndexNow ping failed: {exc}"}
    finally:
        if own_client:
            http.close()


def _draft_parts(package: dict) -> tuple[str, str, str | None, object]:
    """Tolerant extraction of (title, html, excerpt, jsonld) from a drafts.package."""
    article = package.get("article") if isinstance(package.get("article"), dict) else {}
    meta = package.get("meta") if isinstance(package.get("meta"), dict) else {}
    title = article.get("title") or package.get("title") or "Untitled draft"
    html = (
        article.get("html") or article.get("content_html")
        or package.get("html") or package.get("content_html") or ""
    )
    excerpt = (
        meta.get("description") or article.get("excerpt") or package.get("excerpt") or None
    )
    jsonld = package.get("jsonld") if package.get("jsonld") is not None else article.get("jsonld")
    return title, html, excerpt, jsonld


def publish_content_item(
    conn: psycopg.Connection,
    *,
    content_item_id,
    draft_id=None,
    wp: WpClient | None = None,
    indexnow_client: httpx.Client | None = None,
    job_id: int | None = None,
) -> dict:
    """Push a draft to WordPress (as a WP draft) and record the publish trail.

    publish_events row + pages upsert (canonicalized) + content_items
    page_id/status='published' + IndexNow ping (fire-and-forget) + verify jobs
    at T+15m / T+72h with idempotency keys.
    """
    item = conn.execute(
        "select * from content_items where id = %s", (content_item_id,)
    ).fetchone()
    if item is None:
        raise LookupError(f"content_item {content_item_id} not found")
    org_id, site_id = item["org_id"], item["site_id"]

    if draft_id is not None:
        draft = conn.execute(
            "select * from drafts where id = %s and content_item_id = %s",
            (draft_id, content_item_id),
        ).fetchone()
    else:
        draft = conn.execute(
            "select * from drafts where content_item_id = %s order by version desc limit 1",
            (content_item_id,),
        ).fetchone()
    if draft is None:
        raise LookupError(f"no draft {draft_id or '(latest)'} for content_item {content_item_id}")

    if wp is None:
        wp = client_from_connection(conn, site_id)
    title, html, excerpt, jsonld = _draft_parts(draft["package"] or {})
    posted = wp.publish_draft(
        title=title, content_html=html, excerpt=excerpt, meta_jsonld=jsonld
    )
    url = posted.get("link")

    publish_event_id = conn.execute(
        "insert into publish_events (org_id, content_item_id, target, external_id, url)"
        " values (%s, %s, 'wordpress', %s, %s) returning id",
        (org_id, content_item_id, str(posted.get("id")), url),
    ).fetchone()["id"]

    page_id = None
    if url:
        page_id = conn.execute(
            "insert into pages (org_id, site_id, url_norm, last_crawled)"
            " values (%s, %s, %s, now())"
            " on conflict (site_id, url_norm) do update set last_crawled = now() returning id",
            (org_id, site_id, canonicalize_url(url)),
        ).fetchone()["id"]
    conn.execute(
        "update content_items set page_id = coalesce(%s, page_id), status = 'published',"
        " updated_at = now() where id = %s",
        (page_id, content_item_id),
    )

    if url:
        ping = indexnow_ping(url, client=indexnow_client)
    else:
        ping = {"sent": False, "note": "publish response carried no link — ping skipped"}
    if not ping.get("sent"):
        log.info("content_item %s: %s", content_item_id, ping.get("note"))

    now = dt.datetime.now(dt.UTC)
    verify_job_ids = [
        enqueue(
            conn,
            type="verify_publish",
            org_id=org_id,
            site_id=site_id,
            payload={"content_item_id": str(content_item_id), "attempt": attempt},
            run_after=now + delay,
            idempotency_key=f"verify_publish:{content_item_id}:{attempt}",
        )
        for attempt, delay in VERIFY_DELAYS.items()
    ]

    # Job payloads are the audit trail for the ping outcome (publish_events has
    # no notes column and fire-and-forget must not add one).
    if job_id is not None:
        conn.execute(
            "update jobs set payload = payload || %s where id = %s",
            (Jsonb({"indexnow": ping, "publish_event_id": str(publish_event_id)}), job_id),
        )

    return {
        "publish_event_id": str(publish_event_id),
        "post": posted,
        "url": url,
        "page_id": str(page_id) if page_id else None,
        "indexnow": ping,
        "verify_job_ids": verify_job_ids,
    }


def handle_publish(ctx) -> None:
    """Job handler for type 'publish': payload {content_item_id, draft_id?}."""
    payload = ctx.job.payload or {}
    content_item_id = payload.get("content_item_id")
    if not content_item_id:
        raise ValueError(f"job {ctx.job.id}: payload missing 'content_item_id'")
    publish_content_item(
        ctx.conn,
        content_item_id=content_item_id,
        draft_id=payload.get("draft_id"),
        job_id=ctx.job.id,
    )
