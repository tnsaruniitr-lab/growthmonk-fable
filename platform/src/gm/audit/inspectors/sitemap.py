"""Sitemap inspector — ported from fable service/scripts/check_sitemap.py.

No subprocess, no curl, no direct network: all transport goes through the
injected ``fetch`` callable (the Fetcher from gm/audit/fetch.py — any callable
``(url) -> FetchResult``-like object with .status_code and .text works, which
is what tests fake). Algorithm preserved from the source script:

- Discovery order: robots.txt ``Sitemap:`` directives (ALL of them; the body is
  passed in, not refetched) -> /sitemap.xml -> /sitemap_index.xml.
- Real XML parsing via xml.etree.ElementTree (entities, CDATA, namespaces).
- Bounded recursion through sitemap indexes (depth 2, 20 sub-sitemaps per
  index, shared seen-set) with per-FILE URL counts for the 50K limit.
- Deterministic MD5-seeded sampling (same target_url -> same sample; 10 URLs).
- Proportional sample grading: 403 = blocked (bot challenge, never a fail on
  its own), >=90% reachable = pass, >=70% = warn, else fail.

Transport differences from the source, by design: probing uses the Fetcher
(GET) rather than HEAD-with-GET-fallback — the HEAD-405 false-failure class
the source worked around cannot occur. Gzip/Content-Encoding handling is the
Fetcher's job; raw ``.xml.gz`` payloads are not gunzipped here and surface as
XML parse errors.
"""

import hashlib
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable

SITEMAP_NAMESPACE = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
MAX_INDEX_DEPTH = 2
MAX_SUBSITEMAPS_PER_INDEX = 20
SAMPLE_SIZE = 10

# Structural type: gm.audit.fetch.Fetcher (built concurrently). Anything
# callable returning an object with .status_code / .text attributes works.
Fetcher = Callable[[str], object]


def _safe_fetch(fetch: Fetcher, url: str) -> tuple[int, str, str]:
    """Call the fetcher, never raise. Returns (status_code, text, error)."""
    try:
        result = fetch(url)
    except Exception as e:  # UnsafeURL, transport errors, redirect limit, ...
        return 0, "", f"{type(e).__name__}: {e}"
    return result.status_code, result.text or "", ""


def parse_sitemap_xml(xml_body: str) -> dict | None:
    """Parse sitemap XML using stdlib xml.etree.ElementTree.
    Returns {'type': 'index' | 'urlset', 'entries': [...]},
    {'parse_error': ...} on bad XML, or None on empty input.

    Handles: &amp; entities, CDATA sections, namespaces, comments.
    Does NOT use regex on XML (the original bug).
    """
    if not xml_body or not xml_body.strip():
        return None

    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as e:
        return {"parse_error": f"{type(e).__name__}: {e}"}

    # Strip namespace from tag name for simpler inspection
    tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

    # Try with namespace first, fall back to no namespace
    def find_all(element, child_tag):
        with_ns = element.findall(f"sm:{child_tag}", SITEMAP_NAMESPACE)
        if with_ns:
            return with_ns
        return element.findall(child_tag)

    def get_text(element, child_tag):
        child = element.find(f"sm:{child_tag}", SITEMAP_NAMESPACE)
        if child is None:
            child = element.find(child_tag)
        return child.text.strip() if child is not None and child.text else None

    if tag == "sitemapindex":
        entries = []
        for sm in find_all(root, "sitemap"):
            loc = get_text(sm, "loc")
            lastmod = get_text(sm, "lastmod")
            if loc:
                entries.append({"loc": loc, "lastmod": lastmod})
        return {"type": "index", "entries": entries}

    elif tag == "urlset":
        entries = []
        for url_el in find_all(root, "url"):
            loc = get_text(url_el, "loc")
            if loc:
                entries.append({
                    "loc": loc,
                    "lastmod": get_text(url_el, "lastmod"),
                    "changefreq": get_text(url_el, "changefreq"),
                    "priority": get_text(url_el, "priority"),
                })
        return {"type": "urlset", "entries": entries}

    else:
        return {"parse_error": f"unexpected root tag: {tag}"}


def discover_sitemap_urls(
    fetch: Fetcher, base_url: str, robots_txt: str | None
) -> tuple[list[str], str]:
    """Try 3 discovery paths in order:
    1. robots.txt Sitemap: directives (ALL of them, not just the first) —
       parsed from the robots_txt body passed by the caller, no refetch
    2. /sitemap.xml
    3. /sitemap_index.xml
    Returns (sitemap_urls, discovered_via) or ([], 'not_discovered').
    """
    parsed = urllib.parse.urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # 1. robots.txt — collect every Sitemap: directive
    if robots_txt:
        sm_urls: list[str] = []
        for line in robots_txt.splitlines():
            line = line.strip()
            if line.lower().startswith("sitemap:"):
                sm_url = line.split(":", 1)[1].strip()
                if sm_url and sm_url not in sm_urls:
                    sm_urls.append(sm_url)
        if sm_urls:
            return sm_urls, "robots_txt_directive"

    # 2. /sitemap.xml
    sm_url = f"{origin}/sitemap.xml"
    code, _, _ = _safe_fetch(fetch, sm_url)
    if 200 <= code < 300:
        return [sm_url], "default_sitemap_xml"

    # 3. /sitemap_index.xml
    sm_url = f"{origin}/sitemap_index.xml"
    code, _, _ = _safe_fetch(fetch, sm_url)
    if 200 <= code < 300:
        return [sm_url], "default_sitemap_index_xml"

    return [], "not_discovered"


def traverse_sitemap(
    fetch: Fetcher,
    sitemap_url: str,
    depth: int = 0,
    seen: set | None = None,
    stats: dict | None = None,
) -> tuple[list[dict], list[str], dict]:
    """Recursively fetch and parse sitemap + sitemap indexes.
    Returns (all_url_entries, errors, stats).

    stats records per-FILE URL counts (the 50K limit is per sitemap file,
    not per aggregated total) and whether traversal was truncated by the
    MAX_SUBSITEMAPS_PER_INDEX / MAX_INDEX_DEPTH bounds.
    """
    if seen is None:
        seen = set()
    if stats is None:
        stats = {"file_url_counts": {}, "truncated": False}
    if sitemap_url in seen:
        return [], [], stats
    if depth > MAX_INDEX_DEPTH:
        stats["truncated"] = True
        return [], [], stats
    seen.add(sitemap_url)

    errors: list[str] = []
    all_entries: list[dict] = []

    code, body, err = _safe_fetch(fetch, sitemap_url)
    if code == 0 or not body:
        errors.append(f'fetch failed for {sitemap_url}: {err or f"HTTP {code}"}')
        return [], errors, stats
    if code >= 400:
        errors.append(f"HTTP {code} for {sitemap_url}")
        return [], errors, stats

    parsed = parse_sitemap_xml(body)
    if parsed is None:
        errors.append(f"empty/invalid XML at {sitemap_url}")
        return [], errors, stats
    if "parse_error" in parsed:
        errors.append(f'parse error at {sitemap_url}: {parsed["parse_error"]}')
        return [], errors, stats

    if parsed["type"] == "urlset":
        stats["file_url_counts"][sitemap_url] = len(parsed["entries"])
        return parsed["entries"], errors, stats

    elif parsed["type"] == "index":
        # Recurse into sub-sitemaps (bounded)
        sub_entries = parsed["entries"][:MAX_SUBSITEMAPS_PER_INDEX]
        if len(parsed["entries"]) > MAX_SUBSITEMAPS_PER_INDEX:
            stats["truncated"] = True
        for sub in sub_entries:
            child_entries, child_errors, stats = traverse_sitemap(
                fetch, sub["loc"], depth + 1, seen, stats)
            all_entries.extend(child_entries)
            errors.extend(child_errors)

    return all_entries, errors, stats


def deterministic_sample(
    entries: list[dict], target_url: str, sample_size: int = SAMPLE_SIZE
) -> list[dict]:
    """Deterministic sampling: for a given target_url, always returns the same
    sample entries from a given entries list. Uses MD5 hash for stable order.
    """
    if len(entries) <= sample_size:
        return entries

    seed = target_url.encode()
    scored = []
    for entry in entries:
        h = hashlib.md5(seed + entry["loc"].encode()).hexdigest()
        scored.append((h, entry))
    scored.sort(key=lambda x: x[0])
    return [e for _, e in scored[:sample_size]]


def normalize_url_for_compare(url: str) -> str:
    """Normalize a URL for sitemap-membership comparison: strip scheme,
    leading 'www.', and trailing slash. https://www.x.com/a/ and
    http://x.com/a compare equal.
    """
    p = urllib.parse.urlparse(url)
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/")
    query = f"?{p.query}" if p.query else ""
    return f"{host}{path}{query}"


def inspect_sitemap(fetch: Fetcher, base_url: str, robots_txt: str | None) -> dict:
    """Run all sitemap checks against base_url (the audited page URL).

    Output shape mirrors the source script's JSON:
    {'sitemap': {...}, 'checks': {...}}.
    """
    checks: dict = {}
    target_url = base_url

    sitemap_urls, discovered_via = discover_sitemap_urls(fetch, base_url, robots_txt)

    if not sitemap_urls:
        for check_id in ("sitemap_reachable", "target_url_in_sitemap",
                         "no_cross_domain_sitemap_entries",
                         "sampled_urls_return_200", "lastmod_coverage",
                         "sitemap_size_compliance"):
            checks[check_id] = {
                "status": "fail",
                "severity": "high",
                "evidence": "Sitemap could not be discovered via robots.txt, "
                            "/sitemap.xml, or /sitemap_index.xml.",
            }
        return {
            "sitemap": {"found": False, "discovered_via": discovered_via},
            "checks": checks,
        }

    # Traverse every discovered sitemap; the seen-set, depth and
    # per-index bounds are shared (global) across all of them.
    sitemap_url = sitemap_urls[0]
    truncated = False
    if len(sitemap_urls) > MAX_SUBSITEMAPS_PER_INDEX:
        sitemap_urls = sitemap_urls[:MAX_SUBSITEMAPS_PER_INDEX]
        truncated = True
    entries: list[dict] = []
    errors: list[str] = []
    seen: set = set()
    stats: dict = {"file_url_counts": {}, "truncated": False}
    for sm_url in sitemap_urls:
        sm_entries, sm_errors, stats = traverse_sitemap(fetch, sm_url, 0, seen, stats)
        entries.extend(sm_entries)
        errors.extend(sm_errors)
    truncated = truncated or stats["truncated"]

    checks["sitemap_reachable"] = {
        "status": "pass" if entries and not errors else
                  "warn" if entries else "fail",
        "severity": "high",
        "evidence": (
            f"Sitemap located via {discovered_via}: {sitemap_url}"
            + (f" (+{len(sitemap_urls) - 1} more declared)"
               if len(sitemap_urls) > 1 else "")
            + f". {len(entries)} URLs parsed."
            + (f' Warnings: {"; ".join(errors[:3])}' if errors else "")
        ),
    }

    target_parsed = urllib.parse.urlparse(target_url)
    target_origin = f"{target_parsed.scheme}://{target_parsed.netloc}"

    # target_url_in_sitemap — normalize BOTH sides (scheme, leading
    # 'www.', trailing slash) so https://www.x.com/ matches https://x.com/
    target_norm = normalize_url_for_compare(target_url)
    matching = next(
        (e for e in entries if normalize_url_for_compare(e["loc"]) == target_norm),
        None,
    )
    if matching:
        evidence = (
            f"Target URL {target_url} found in sitemap"
            + (" (normalized (trailing slash, www, or protocol differs))"
               if matching["loc"] != target_url else "")
            + (f'. lastmod: {matching["lastmod"]}' if matching["lastmod"] else "")
        )
        checks["target_url_in_sitemap"] = {
            "status": "pass", "severity": "high", "evidence": evidence,
        }
    elif truncated:
        checks["target_url_in_sitemap"] = {
            "status": "warn", "severity": "high",
            "evidence": f"Target URL {target_url} not found in the {len(entries)} "
                        f"URLs traversed, but the search was truncated "
                        f"(sub-sitemap/depth bounds hit) — the URL may be in an "
                        f"untraversed sitemap.",
        }
    else:
        checks["target_url_in_sitemap"] = {
            "status": "fail", "severity": "high",
            "evidence": f"Target URL {target_url} not found in sitemap "
                        f"({len(entries)} URLs checked).",
        }

    # no_cross_domain_sitemap_entries
    cross_domain = []
    for entry in entries[:500]:  # sample for performance
        p = urllib.parse.urlparse(entry["loc"])
        entry_origin = f"{p.scheme}://{p.netloc}"
        if entry_origin != target_origin:
            cross_domain.append(entry["loc"])
    checks["no_cross_domain_sitemap_entries"] = {
        "status": "pass" if not cross_domain else "warn",
        "severity": "medium",
        "evidence": (
            f"All sitemap entries point to the expected origin ({target_origin})."
            if not cross_domain
            else f"{len(cross_domain)} URLs point to different origins. "
                 f"Examples: {cross_domain[:3]}"
        ),
    }

    # sampled_urls_return_200 — probed through the injected Fetcher (GET).
    # Graded by PROPORTION of reachable sampled URLs, not a single failure: a
    # lone dead URL (a stale sitemap entry) in an otherwise-healthy sample is
    # a warning, not a whole-check fail. 403s are treated as "blocked"
    # (bot-challenge), which keep warn semantics and are excluded from the
    # reachable count without counting as dead.
    sample = deterministic_sample(entries, target_url, sample_size=SAMPLE_SIZE)
    sample_results = []
    dead = []
    blocked = []
    reachable = 0
    for entry in sample:
        code, _, _ = _safe_fetch(fetch, entry["loc"])
        sample_results.append({"url": entry["loc"], "code": code, "method": "GET"})
        if code == 403:
            # 403 usually means a bot challenge / WAF, not a dead URL
            blocked.append((entry["loc"], code))
        elif 200 <= code < 400:
            reachable += 1
        else:
            dead.append((entry["loc"], code))

    total_sampled = len(sample)
    reachable_ratio = (reachable / total_sampled) if total_sampled else 0.0
    ratio_pct = round(reachable_ratio * 100)

    # Proportional grade. Blocked-only samples (no dead URLs) never fail —
    # a WAF challenge is inconclusive, not a broken sitemap.
    if dead:
        if reachable_ratio >= 0.9:
            status = "pass"
        elif reachable_ratio >= 0.7:
            status = "warn"
        else:
            status = "fail"
    elif blocked:
        status = "warn"
    else:
        status = "pass"

    if dead:
        sample_evidence = (
            f"{reachable} of {total_sampled} sampled URLs reachable ({ratio_pct}%). "
            f"{len(dead)} returned an error status (outside 200-399): {dead[:3]}"
            + (f". {len(blocked)} additionally blocked with HTTP 403 "
               f"(likely bot challenge): {blocked[:3]}" if blocked else "")
        )
    elif blocked:
        sample_evidence = (
            f"{reachable} of {total_sampled} sampled URLs reachable ({ratio_pct}%); "
            f"{len(blocked)} returned HTTP 403 — blocked (likely bot challenge), "
            f"not necessarily dead: {blocked[:3]}"
        )
    else:
        sample_evidence = (
            f"All {total_sampled} sampled URLs reachable ({ratio_pct}%) "
            f"(HTTP 200-399)."
        )
    checks["sampled_urls_return_200"] = {
        "status": status,
        "severity": "high",
        "evidence": sample_evidence,
        "detail": {
            "reachable": reachable,
            "total_sampled": total_sampled,
            "reachable_ratio": round(reachable_ratio, 3),
            "dead_urls": dead[:10],
            "blocked_urls": blocked[:10],
            "sample_results": sample_results,
        },
    }

    # lastmod_coverage
    with_lastmod = sum(1 for e in entries if e.get("lastmod"))
    coverage = (with_lastmod / len(entries) * 100) if entries else 0
    checks["lastmod_coverage"] = {
        "status": "pass" if coverage >= 80 else
                  "warn" if coverage >= 40 else "fail",
        "severity": "medium",
        "evidence": f"{with_lastmod}/{len(entries)} URLs ({coverage:.0f}%) "
                    f"have lastmod dates.",
    }

    # sitemap_size_compliance (Google limits: 50K URLs PER FILE, 50MB)
    oversized_files = sorted(
        url for url, count in stats["file_url_counts"].items() if count > 50_000
    )
    file_count = len(stats["file_url_counts"])
    checks["sitemap_size_compliance"] = {
        "status": "pass" if not oversized_files else "warn",
        "severity": "low",
        "evidence": (
            f"No sitemap file exceeds 50,000 URLs ({file_count} file(s), "
            f"{len(entries)} URLs total; Google limit is per file)."
            if not oversized_files
            else f"{len(oversized_files)} sitemap file(s) exceed the 50,000-URL "
                 f"per-file limit: {oversized_files[:3]} — split into multiple sitemaps."
        ),
    }

    return {
        "sitemap": {
            "found": True,
            "sitemap_url": sitemap_url,
            "sitemap_urls": sitemap_urls,
            "discovered_via": discovered_via,
            "total_urls_indexed": len(entries),
            "truncated": truncated,
            "traversal_errors": errors[:5] if errors else [],
        },
        "checks": checks,
    }
