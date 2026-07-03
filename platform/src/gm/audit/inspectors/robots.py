"""Robots.txt inspector — ported from fable service/scripts/check_robots_txt.py.

Pure function of its inputs: the caller fetches robots.txt and passes the body
(or None when the file was unreachable / returned an HTTP error). No network,
no subprocess. Algorithm preserved from the source script:

- RFC 9309 group parsing (BOM/CRLF tolerance, comments, empty-body tolerance,
  groups committed even with zero Allow/Disallow rules).
- UA precedence: a group token matches when it is a case-insensitive PREFIX of
  the crawler's product token; the longest matching token wins; wildcard (*)
  groups apply only when no specific token matches.
- Path evaluation: longest matching pattern wins, Allow wins ties, '*' matches
  any sequence, trailing '$' anchors, empty Disallow means allow-all.
"""

import re
import urllib.parse

# Bots we evaluate for explicit allow/deny (fable's 16-bot list, order preserved)
BOTS_TO_CHECK = [
    "Googlebot", "Bingbot", "BingPreview",
    "GPTBot", "ChatGPT-User", "OAI-SearchBot",
    "ClaudeBot", "Claude-Web", "anthropic-ai",
    "PerplexityBot",
    "Google-Extended", "Applebot", "Applebot-Extended",
    "CCBot", "DuckDuckBot", "Bytespider",
]

AI_CRAWLERS_ONLY = [
    "GPTBot", "ChatGPT-User", "OAI-SearchBot",
    "ClaudeBot", "Claude-Web", "anthropic-ai",
    "PerplexityBot", "Google-Extended",
    "CCBot", "Applebot-Extended",
]

_EMPTY_PARSE: dict = {"groups": [], "sitemaps": [], "empty": True, "parse_warnings": []}


def parse_robots_txt(body: str | None) -> dict:
    """Parse robots.txt into groups. Each group has:
      - user_agents: list of UA tokens this group applies to
      - rules: list of (directive_type, path) tuples

    Tolerates empty body, BOM, windows line endings, stray whitespace, and
    comments. Does NOT raise — returns a valid (possibly-empty) structure.
    """
    result: dict = {"groups": [], "sitemaps": [], "empty": False, "parse_warnings": []}

    if body is None or not body.strip():
        result["empty"] = True
        return result

    body = body.lstrip("\ufeff")
    body = body.replace("\r\n", "\n").replace("\r", "\n")

    current_uas: list[str] = []
    current_rules: list[tuple[str, str]] = []
    last_was_ua = False

    def commit_group() -> None:
        # Commit any group that names user-agents — even with zero
        # Allow/Disallow rules (e.g. only Crawl-delay). An empty rule
        # list means allow-all for those UAs; dropping the group would
        # wrongly let the bot fall through to the wildcard group.
        if current_uas:
            result["groups"].append({
                "user_agents": list(current_uas),
                "rules": list(current_rules),
            })

    for line_no, raw_line in enumerate(body.split("\n"), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        if ":" not in line:
            result["parse_warnings"].append(
                f'line {line_no}: missing colon, skipped: "{raw_line.strip()[:60]}"'
            )
            continue

        directive, value = line.split(":", 1)
        directive = directive.strip().lower()
        value = value.strip()

        if directive == "sitemap":
            if value:
                result["sitemaps"].append(value)
            continue

        if directive == "user-agent":
            # If previous line was a rule (not a UA), we're starting a new group
            if not last_was_ua and (current_uas or current_rules):
                commit_group()
                current_uas = []
                current_rules = []
            if value:
                current_uas.append(value)
            last_was_ua = True
            continue

        if directive in ("disallow", "allow"):
            if not current_uas:
                result["parse_warnings"].append(
                    f"line {line_no}: {directive} before user-agent, ignored"
                )
                continue
            # RFC 9309: empty Disallow means allow-all; empty Allow is a no-op
            current_rules.append((directive, value))
            last_was_ua = False
            continue

        if directive in ("crawl-delay", "host", "noindex", "clean-param"):
            last_was_ua = False
            continue

        result["parse_warnings"].append(
            f'line {line_no}: unknown directive "{directive}", ignored'
        )

    commit_group()
    return result


def find_matching_groups(parsed: dict, user_agent: str) -> list[dict]:
    """Find groups matching the given user_agent, per RFC 9309 §2.2.1 and
    Google's robotstxt parser: a group token matches a crawler when the
    token is a case-insensitive PREFIX of the crawler's product token
    (group 'googlebot' matches crawler 'Googlebot-Image', but group
    'googlebot-image' does NOT match crawler 'Googlebot').
    The longest matching token wins; wildcard (*) groups apply only when
    no specific token matches.
    """
    ua_lower = user_agent.lower()
    wildcard: list[dict] = []
    best_len = -1
    best_groups: list[dict] = []
    for group in parsed.get("groups", []):
        group_match_len = -1
        for g_ua in group["user_agents"]:
            g_lower = g_ua.lower()
            if g_lower == "*":
                wildcard.append(group)
            elif ua_lower.startswith(g_lower):
                group_match_len = max(group_match_len, len(g_lower))
        if group_match_len < 0:
            continue
        if group_match_len > best_len:
            best_len = group_match_len
            best_groups = [group]
        elif group_match_len == best_len:
            best_groups.append(group)
    return best_groups if best_groups else wildcard


def evaluate_path_access(groups: list[dict], path: str) -> tuple[bool, str]:
    """RFC 9309 evaluation: longest matching path wins; Allow wins ties.
    Returns (allowed, evidence). No matching groups -> allowed (permissive).
    """
    if not groups:
        return True, "no matching rule groups — permissive default applied"

    best_match_len = -1
    best_directive = None
    best_pattern = None

    for group in groups:
        for directive, pattern in group["rules"]:
            # Empty Disallow means "no disallow rules" — allow all
            if directive == "disallow" and not pattern:
                if best_match_len < 0:
                    best_match_len = 0
                    best_directive = "allow"
                    best_pattern = "(empty Disallow = allow-all)"
                continue
            if not pattern:
                continue

            # RFC 9309 §2.2.3: '*' matches any sequence of characters,
            # trailing '$' anchors the match to the end of the path.
            anchored = pattern.endswith("$")
            core = pattern[:-1] if anchored else pattern
            regex = re.escape(core).replace(r"\*", ".*")
            if anchored:
                regex += "$"
            try:
                matched = re.match(regex, path) is not None
            except re.error:
                continue
            if not matched:
                continue
            if len(pattern) > best_match_len:
                best_match_len = len(pattern)
                best_directive = directive
                best_pattern = pattern
            elif len(pattern) == best_match_len and directive == "allow":
                # Allow wins tie
                best_directive = "allow"
                best_pattern = pattern

    if best_directive is None:
        return True, "no matching rule — permissive default"
    allowed = best_directive == "allow"
    return allowed, f'{best_directive} pattern "{best_pattern}" (length {best_match_len})'


def _target_path(target_url: str) -> str:
    parsed_url = urllib.parse.urlparse(target_url)
    target_path = parsed_url.path or "/"
    # Rules can match on the query string too (e.g. Disallow: /*?print=1)
    if parsed_url.query:
        target_path += "?" + parsed_url.query
    return target_path


def inspect_robots(robots_txt: str | None, target_url: str) -> dict:
    """Run all robots.txt checks for target_url against the given robots body.

    robots_txt=None means the file was unreachable or returned an HTTP error
    (the caller owns transport). Output shape mirrors the source script's
    JSON: {'robots_txt': {...}, 'bots': {...}, 'checks': {...}} — 'http_code'
    is omitted (no fetch happens here) and 'bots' adds per-bot allow/deny.
    """
    checks: dict = {}
    target_path = _target_path(target_url)

    # --- robots_reachable ---
    if robots_txt is None:
        checks["robots_reachable"] = {
            "status": "fail", "severity": "high",
            "evidence": "robots.txt unavailable (fetch failed or HTTP error). "
                        "Per RFC 9309 §2.3.1.3, 4xx means \"no robots.txt\" and "
                        "crawlers apply permissive default; 5xx means crawlers "
                        "assume complete disallow.",
        }
        parsed = dict(_EMPTY_PARSE)
        robots_available = False
    elif not robots_txt.strip():
        checks["robots_reachable"] = {
            "status": "warn", "severity": "low",
            "evidence": "robots.txt reachable but empty. "
                        "All user-agents allowed (permissive default).",
        }
        parsed = parse_robots_txt(robots_txt)
        robots_available = True
    else:
        parsed = parse_robots_txt(robots_txt)
        checks["robots_reachable"] = {
            "status": "pass", "severity": "info",
            "evidence": f"robots.txt reachable, {len(robots_txt)} bytes. "
                        f'{len(parsed["groups"])} user-agent group(s), '
                        f'{len(parsed["sitemaps"])} sitemap directive(s).',
        }
        robots_available = True

    # --- robots_declares_sitemap ---
    if not robots_available:
        checks["robots_declares_sitemap"] = {
            "status": "na", "severity": "medium",
            "evidence": "Cannot evaluate Sitemap: declarations — robots.txt "
                        "is unreachable or returned an error.",
        }
    elif parsed["sitemaps"]:
        checks["robots_declares_sitemap"] = {
            "status": "pass", "severity": "info",
            "evidence": f'robots.txt declares {len(parsed["sitemaps"])} sitemap(s): '
                        f'{parsed["sitemaps"][:3]}',
        }
    else:
        checks["robots_declares_sitemap"] = {
            "status": "warn", "severity": "medium",
            "evidence": "robots.txt does not declare any Sitemap: directive. "
                        "Crawlers must rely on /sitemap.xml convention.",
        }

    # --- per-bot allow/deny (all 16 bots) ---
    bots: dict = {}
    if robots_available:
        for bot in BOTS_TO_CHECK:
            groups = find_matching_groups(parsed, bot)
            explicit = any(
                any(ua.lower() == bot.lower() for ua in g["user_agents"])
                for g in groups
            )
            allowed, evidence = evaluate_path_access(groups, target_path)
            bots[bot] = {"allowed": allowed, "explicit": explicit, "rule": evidence}

    # --- googlebot_allowed ---
    if robots_available:
        gbot = bots["Googlebot"]
        gbot_explicit = gbot["explicit"]
        allowed = gbot["allowed"]
        checks["googlebot_allowed"] = {
            "status": "pass" if allowed else "fail",
            "severity": "critical" if not allowed else "info",
            "evidence": (
                f'Googlebot {"explicitly listed and " if gbot_explicit else ""}'
                f"allowed for {target_path}." if allowed
                else f'Googlebot DISALLOWED for {target_path}. Rule: {gbot["rule"]}'
            ),
        }
    else:
        checks["googlebot_allowed"] = {
            "status": "warn", "severity": "medium",
            "evidence": "Cannot evaluate Googlebot access — robots.txt inaccessible. "
                        "Permissive default assumes allowed, but should be verified.",
        }

    # --- ai_crawlers_all_allowed ---
    if robots_available:
        ai_explicit = [b for b in AI_CRAWLERS_ONLY if bots[b]["explicit"]]
        ai_denied = [b for b in AI_CRAWLERS_ONLY if not bots[b]["allowed"]]

        if ai_denied:
            checks["ai_crawlers_all_allowed"] = {
                "status": "fail", "severity": "high",
                "evidence": f"{len(ai_denied)} AI crawlers DENIED access: {ai_denied}",
            }
        elif len(ai_explicit) == len(AI_CRAWLERS_ONLY):
            checks["ai_crawlers_all_allowed"] = {
                "status": "pass", "severity": "info",
                "evidence": f"All {len(AI_CRAWLERS_ONLY)} AI crawlers explicitly allowed.",
            }
        else:
            missing = [b for b in AI_CRAWLERS_ONLY if b not in ai_explicit]
            checks["ai_crawlers_all_allowed"] = {
                "status": "warn", "severity": "low",
                "evidence": (
                    f"{len(ai_explicit)} of {len(AI_CRAWLERS_ONLY)} AI crawlers "
                    f"explicitly listed: {ai_explicit}. Others ({missing}) allowed "
                    f"only via wildcard — consider explicit entries for clarity."
                ),
            }
    else:
        checks["ai_crawlers_all_allowed"] = {
            "status": "warn", "severity": "medium",
            "evidence": "Cannot evaluate AI crawler access — robots.txt inaccessible.",
        }

    # --- target_path_not_disallowed (across all bots checked) ---
    if robots_available:
        blocked_for = [b for b in BOTS_TO_CHECK if not bots[b]["allowed"]]
        if blocked_for:
            checks["target_path_not_disallowed"] = {
                "status": "fail", "severity": "high",
                "evidence": f"Target path {target_path} blocked for: {blocked_for}",
            }
        else:
            checks["target_path_not_disallowed"] = {
                "status": "pass", "severity": "info",
                "evidence": f"Target path {target_path} is allowed for all "
                            f"{len(BOTS_TO_CHECK)} checked bots.",
            }
    else:
        checks["target_path_not_disallowed"] = {
            "status": "warn", "severity": "medium",
            "evidence": "Cannot evaluate target path access — robots.txt inaccessible. "
                        "Permissive default applied.",
        }

    return {
        "robots_txt": {
            "provided": robots_txt is not None,
            "reachable": robots_available,
            "body_size": len(robots_txt) if robots_txt else 0,
            "groups_count": len(parsed.get("groups", [])),
            "sitemaps_declared": parsed.get("sitemaps", []),
            "parse_warnings": parsed.get("parse_warnings", [])[:5],
        },
        "bots": bots,
        "checks": checks,
    }
