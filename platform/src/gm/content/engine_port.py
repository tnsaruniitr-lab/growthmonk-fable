"""Content-engine port (docs/phase-c-wave3-contracts.md, agent A).

The content engine is the EXISTING serp-analyzer Express service (sibling
repo, READ-ONLY), reached via CONTENT_ENGINE_URL (+ CONTENT_ENGINE_TOKEN
bearer if set). Request/response shapes come from that repo's
src/blog/types.ts (BlogWriterRequestSchema zod) and src/routes/blog.ts
(POST /blog/write-and-audit -> {blog_package, audit, iterations, ...}).

The convergence fix is REQUEST-SIDE (docs/convergence-diagnosis.md): every
request carries enforce_human_signals=True plus the full E-E-A-T bundle —
a real author entity with a linkedin sameAs from sites.author, real
first-party data from sites.first_party, and named examples / editorial
stance / sources / an original-visual spec grounded in the brief's own SERP
and gap data. NOTHING is ever invented: a missing input FAILS FAST with a
named-field error (an ungrounded draft is the bug we just diagnosed, not a
degraded mode).
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

WRITE_AND_AUDIT_PATH = "/blog/write-and-audit"
DEFAULT_TIMEOUT = 900.0

# Contract-exact fragment for the empty-author fail-fast path.
AUTHOR_MISSING_MSG = "set author first: gm site set-author"

# zod minimums recorded from serp-analyzer/src/blog/types.ts — keep in sync.
_BIO_MIN_CHARS = 30            # BlogAuthorSchema.bio
_TEXT_MIN_CHARS = 20           # finding/observation/claim/description min(20)
_MIN_NAMED_EXAMPLES = 3        # superRefine: named_examples >= 3
_MIN_PRIMARY_SOURCES = 3       # superRefine: 'primary' authority-tier sources >= 3
_TARGET_WORD_COUNT = 1800      # diagnosis: >= 1400 avoids S_word_count_below_band
_SECONDARY_KEYWORDS_CAP = 10
_INTENTS = frozenset({"informational", "commercial", "transactional", "navigational"})

_METRIC_RE = re.compile(r"\d[\d,.]*\s*(?:%|percent|pp|x)?", re.IGNORECASE)


class EngineUnavailable(Exception):
    """Engine unreachable or misconfigured: the job fails with the honest
    error and is retryable by re-enqueue."""


class RequestFieldMissing(ValueError):
    """A zod-required field cannot be grounded in real site/brief data."""

    def __init__(self, field: str, detail: str):
        self.field = field
        super().__init__(f"close_fixes: required field '{field}' missing — {detail}")


def _fail(field: str, detail: str) -> None:
    raise RequestFieldMissing(field, detail)


# ---------------------------------------------------------------------------
# Request assembly — pure functions on the sites row + briefs row
# ---------------------------------------------------------------------------

def _is_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _author_entity(author: Any) -> dict:
    """sites.author jsonb -> BlogAuthorSchema. Empty author fails fast with
    the contract-exact message; partial authors fail on the named field."""
    if not isinstance(author, dict) or not any(
        isinstance(v, str | list) and v for v in author.values()
    ):
        _fail("author", AUTHOR_MISSING_MSG)
    name = str(author.get("name") or "").strip()
    if not name:
        _fail("author.name", "sites.author has no name")
    title = str(author.get("title") or "").strip()
    if not title:
        _fail("author.title", "sites.author has no title/role")
    same_as = [s for s in (author.get("sameAs") or []) if isinstance(s, str)]
    linkedin = next((s for s in same_as if "linkedin.com" in s.lower() and _is_url(s)), None)
    if linkedin is None:
        _fail("author.linkedin_url", "sites.author.sameAs has no linkedin.com URL")
    bio = str(author.get("bio") or author.get("credentials") or "").strip()
    if len(bio) < _BIO_MIN_CHARS:
        _fail("author.bio",
              f"sites.author bio/credentials must be >= {_BIO_MIN_CHARS} chars (got {len(bio)})")
    entity: dict[str, Any] = {
        "name": name, "title": title, "bio": bio, "linkedin_url": linkedin,
    }
    twitter = next(
        (s for s in same_as
         if _is_url(s) and any(h in s.lower() for h in ("twitter.com", "x.com"))),
        None,
    )
    if twitter:
        entity["twitter_url"] = twitter
    expertise = author.get("expertise") or author.get("expertise_keywords")
    if isinstance(expertise, list):
        keywords = [k for k in expertise if isinstance(k, str) and k.strip()]
        if keywords:
            entity["expertise_keywords"] = keywords
    return entity


def _first_party_data(first_party: Any) -> list[dict]:
    """sites.first_party [{fact, source}] -> FirstPartyDataSchema list.
    Real facts only; entries without a grounded fact+source are dropped, and
    an empty result fails fast (never invented)."""
    out: list[dict] = []
    for entry in first_party if isinstance(first_party, list) else []:
        if not isinstance(entry, dict):
            continue
        fact = str(entry.get("fact") or entry.get("finding") or "").strip()
        source = str(entry.get("source") or entry.get("source_description") or "").strip()
        if len(fact) < _TEXT_MIN_CHARS or not source:
            continue
        metric = str(entry.get("metric") or "").strip()
        if not metric:
            match = _METRIC_RE.search(fact)
            # honest fallback: name the number found in the fact, or say so
            metric = match.group(0).strip() if match else "unquantified"
        row = {"finding": fact, "metric": metric, "source_description": source}
        if isinstance(entry.get("collected_at"), str):
            row["collected_at"] = entry["collected_at"]
        out.append(row)
    if not out:
        _fail("first_party_data",
              "sites.first_party has no usable entries (each needs a fact of"
              f" >= {_TEXT_MIN_CHARS} chars and its source); real data only")
    return out


def _serp_rows(brief: dict, client_domain: str) -> list[dict]:
    rows = []
    for row in brief.get("serp_table") or []:
        if not isinstance(row, dict):
            continue
        domain = str(row.get("domain") or "").lower()
        if not domain or domain == client_domain.lower():
            continue
        rows.append(row)
    return rows


def _sources(brief: dict, client_domain: str) -> list[dict]:
    """BlogSourceSchema list: registry citations on the required fixes
    (genuinely primary-tier: Google/W3C/etc. docs) plus the brief's SERP
    competitor URLs (primary-tier per the wave-3 contract)."""
    query = str(brief.get("query_norm") or "").strip()
    sources: list[dict] = []
    seen: set[str] = set()
    for fix in brief.get("required_fixes") or []:
        if not isinstance(fix, dict):
            continue
        for citation in fix.get("citations") or []:
            if not isinstance(citation, dict):
                continue
            url = citation.get("source_url")
            if not _is_url(url) or url in seen:
                continue
            seen.add(url)
            title = str(citation.get("title") or url)
            source = {
                "id": f"src-{len(sources) + 1}",
                "title": title,
                "url": url,
                "excerpt": title,
                "authority_tier": "primary",
            }
            org = str(citation.get("source_org") or "").strip()
            if org:
                source["publisher"] = org
            sources.append(source)
    for row in _serp_rows(brief, client_domain):
        url = row.get("url")
        if not _is_url(url) or url in seen:
            continue
        seen.add(url)
        rank = row.get("rank")
        rank_note = f"ranked #{rank}" if isinstance(rank, int) else "in the top results"
        title = str(row.get("title") or url)
        sources.append({
            "id": f"src-{len(sources) + 1}",
            "title": title,
            "url": url,
            "excerpt": f'{rank_note} on Google for "{query}" (brief SERP snapshot)',
            "authority_tier": "primary",
        })
    primary = sum(1 for s in sources if s["authority_tier"] == "primary")
    if not sources or primary < _MIN_PRIMARY_SOURCES:
        _fail("sources",
              f"need >= {_MIN_PRIMARY_SOURCES} primary-tier sources; the brief's"
              f" fix citations + SERP table ground only {primary}")
    return sources


def _named_examples(brief: dict, query: str, client_domain: str) -> list[dict]:
    """NamedExampleSchema list from the brief's own SERP snapshot — real
    brands with their observed Google ranks, never invented."""
    out: list[dict] = []
    for row in _serp_rows(brief, client_domain):
        domain = str(row.get("domain") or "")
        rank = row.get("rank")
        title = str(row.get("title") or "").strip()
        rank_note = f"ranks #{rank}" if isinstance(rank, int) else "appears in the top results"
        observation = (
            f'{rank_note} on Google for "{query}"'
            + (f' with the page titled "{title}"' if title else "")
            + " (brief SERP snapshot)."
        )
        example: dict[str, Any] = {"brand": domain, "observation": observation}
        if isinstance(rank, int):
            example["metric"] = f"Google rank #{rank}"
        if _is_url(row.get("url")):
            example["source_url"] = row["url"]
        out.append(example)
    if len(out) < _MIN_NAMED_EXAMPLES:
        _fail("named_examples",
              f"need >= {_MIN_NAMED_EXAMPLES} real examples; the brief's SERP"
              f" table grounds only {len(out)} competitor rows")
    return out


def _editorial_stance(brief: dict, query: str) -> dict:
    """EditorialStanceSchema from the brief's own synthesis angle, else from
    its comparison gaps. No grounded stance -> fail fast."""
    synthesis = brief.get("synthesis") if isinstance(brief.get("synthesis"), dict) else {}
    angle = str(synthesis.get("angle") or "").strip()
    if len(angle) >= _TEXT_MIN_CHARS:
        return {
            "claim": angle,
            "supporting_reasoning":
                f'Grounded in the brief\'s SERP and competitor-gap research for "{query}".',
        }
    gaps = [g for g in (brief.get("gaps") or []) if isinstance(g, dict)]
    if gaps:
        gap = gaps[0]
        name = str(gap.get("name") or gap.get("check_id") or "a key check")
        passing = gap.get("competitors_passing")
        return {
            "claim":
                f'Pages that pass "{name}" dominate the Google results for "{query}" —'
                " matching them on it is table stakes before anything else matters.",
            "supporting_reasoning":
                f'{passing if passing is not None else "Several"} audited competitor(s)'
                f' pass "{name}" while the target page fails it (brief gap analysis).',
        }
    _fail("editorial_stance",
          "brief has neither a synthesis angle nor comparison gaps to ground a stance")
    raise AssertionError("unreachable")  # pragma: no cover


def _original_visuals(brief: dict, query: str) -> list[dict]:
    """OriginalVisualSchema list: commission ONE visual built from the
    brief's own data (a spec for a chart, not a fabricated asset)."""
    serp_rows = [r for r in (brief.get("serp_table") or []) if isinstance(r, dict)]
    if serp_rows:
        return [{
            "type": "chart",
            "placement_hint": "after the introduction",
            "description":
                f'Comparison chart of the top {len(serp_rows)} Google results for'
                f' "{query}" (rank, title, audit score), built from the brief\'s'
                " SERP snapshot data.",
        }]
    fixes = [f for f in (brief.get("required_fixes") or []) if isinstance(f, dict)]
    if fixes:
        return [{
            "type": "framework",
            "placement_hint": "alongside the recommendations section",
            "description":
                f"Checklist framework of the {len(fixes)} audited on-page fixes"
                " from the brief's required-fix list, with pass/fail status.",
        }]
    _fail("original_visuals",
          "brief has no SERP table or required fixes to build a visual spec from")
    raise AssertionError("unreachable")  # pragma: no cover


def _brand(site: dict) -> dict:
    domain = str(site.get("domain_norm") or "").strip()
    if not domain:
        _fail("brand.domain", "sites.domain_norm is empty")
    terms = [t for t in (site.get("brand_terms") or []) if isinstance(t, str) and t.strip()]
    description = str(site.get("notes") or "").strip()
    if not description:
        _fail("brand.product_description",
              "sites.notes is empty — set site notes describing the product/service")
    return {
        "name": terms[0] if terms else domain,
        "domain": domain,
        "product_description": description,
    }


def _slug_from_page(page_url: str) -> str | None:
    path = urlsplit(page_url).path.strip("/")
    if not path:
        return None
    return path.split("/")[-1] or None


def build_writer_request(site: dict, brief_row: dict, *, kind: str) -> dict:
    """brief jsonb + sites.author/first_party/brand_terms -> a schema-valid
    BlogWriterRequest with enforce_human_signals=True. Pure function.

    Mapping (types.ts field for field): topic/keywords/intent from the
    brief's target + synthesis; PAA questions -> secondary_keywords (the zod
    schema has no separate questions field; include_faq consumes them);
    sources from the fix-citation + SERP tables; author entity + first-party
    data from the sites columns; named examples / stance / visual spec
    grounded in the brief's own SERP and gap data. Missing inputs raise
    RequestFieldMissing — facts are never invented.
    """
    target = brief_row.get("target") if isinstance(brief_row.get("target"), dict) else {}
    brief = brief_row.get("brief") if isinstance(brief_row.get("brief"), dict) else {}
    query = str(target.get("query") or brief.get("query_norm") or "").strip()
    if not query:
        _fail("topic", "brief target has no query")
    synthesis = brief.get("synthesis") if isinstance(brief.get("synthesis"), dict) else {}

    brand = _brand(site)
    author = _author_entity(site.get("author"))
    secondary: list[str] = []
    for row in brief.get("paa") or []:
        question = row.get("question") if isinstance(row, dict) else row
        if isinstance(question, str) and question.strip():
            secondary.append(question.strip())
    intent = str(target.get("intent") or "").lower()

    article: dict[str, Any] = {
        "target_word_count": _TARGET_WORD_COUNT,
        "include_faq": True,
        "author_name": author["name"],
    }
    page = target.get("page")
    if kind == "refresh" and isinstance(page, str) and page:
        slug = _slug_from_page(page)
        if slug:
            article["slug"] = slug

    request: dict[str, Any] = {
        "topic": str(synthesis.get("title") or "").strip() or query,
        "primary_keyword": query,
        "secondary_keywords": secondary[:_SECONDARY_KEYWORDS_CAP],
        "search_intent": intent if intent in _INTENTS else "informational",
        "brand": brand,
        "sources": _sources(brief, brand["domain"]),
        "article": article,
        "enforce_human_signals": True,
        "author": author,
        "first_party_data": _first_party_data(site.get("first_party")),
        "named_examples": _named_examples(brief, query, brand["domain"]),
        "editorial_stance": _editorial_stance(brief, query),
        "original_visuals": _original_visuals(brief, query),
    }
    angle = str(synthesis.get("angle") or "").strip()
    if angle:
        request["angle"] = angle
    return request


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class ContentEngine:
    """Thin HTTP port onto the serp-analyzer service."""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 client: httpx.Client | None = None):
        self.base_url = (base_url or os.environ.get("CONTENT_ENGINE_URL") or "").rstrip("/")
        if not self.base_url:
            raise EngineUnavailable(
                "CONTENT_ENGINE_URL is not set — content engine unavailable"
            )
        self.token = token if token is not None else os.environ.get("CONTENT_ENGINE_TOKEN")
        self._client = client

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        return headers

    def write_and_audit(self, request: dict, *, timeout: float = DEFAULT_TIMEOUT) -> dict:
        """POST /blog/write-and-audit -> their package + audit dict.

        Transport failures and 5xx raise EngineUnavailable (retryable by
        re-enqueue). A 400 is OUR schema bug — surfaced as ValueError with
        the engine's zod details, never masked as engine-down."""
        url = self.base_url + WRITE_AND_AUDIT_PATH
        try:
            if self._client is not None:
                resp = self._client.post(
                    url, json=request, headers=self._headers(), timeout=timeout
                )
            else:  # pragma: no cover — real network client; tests always inject
                with httpx.Client(timeout=timeout) as client:
                    resp = client.post(url, json=request, headers=self._headers())
        except httpx.HTTPError as exc:
            raise EngineUnavailable(
                f"content engine unreachable at {url}: {type(exc).__name__}: {exc}"
            ) from exc
        if resp.status_code == 400:
            raise ValueError(
                f"content engine rejected the request (400 — our schema bug,"
                f" not engine-down): {resp.text[:1000]}"
            )
        if resp.status_code != 200:
            raise EngineUnavailable(
                f"content engine error {resp.status_code} at {url}: {resp.text[:300]}"
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise EngineUnavailable(
                f"content engine returned non-JSON at {url}: {resp.text[:200]}"
            ) from exc
        if not isinstance(body, dict):
            raise EngineUnavailable(f"content engine returned non-object JSON at {url}")
        return body
