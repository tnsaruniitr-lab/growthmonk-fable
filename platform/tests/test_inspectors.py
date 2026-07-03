"""Tests for gm.audit.inspectors (robots, sitemap, schema_markup).

No network, no DB: robots/schema inspectors are pure string functions; the
sitemap inspector gets a fake Fetcher built on an in-memory URL map. Fixtures
under tests/fixtures/inspectors/ are ported from the source repos'
regression suites (aeo-seo-auditor-fable/tests/fixtures — XML entities/CDATA
sitemap, empty robots, 403-robots expectations, SSR landing page with
JSON-LD).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from gm.audit.inspectors.robots import (
    AI_CRAWLERS_ONLY,
    BOTS_TO_CHECK,
    evaluate_path_access,
    find_matching_groups,
    inspect_robots,
    parse_robots_txt,
)
from gm.audit.inspectors.schema_markup import (
    FIELD_SPECS,
    extract_schema_blocks,
    flatten_entities,
    inspect_schema,
    normalize_type,
)
from gm.audit.inspectors.sitemap import (
    deterministic_sample,
    inspect_sitemap,
    normalize_url_for_compare,
)

FIXTURES = Path(__file__).parent / "fixtures" / "inspectors"


# ---------------------------------------------------------------------------
# Fake fetcher (contract FetchResult shape; real impl built by another agent)
# ---------------------------------------------------------------------------

@dataclass
class FakeFetchResult:
    url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    text: str
    elapsed_ms: int = 0
    redirect_chain: list[str] = field(default_factory=list)


def make_fake_fetcher(pages: dict[str, tuple[int, str]], calls: list[str] | None = None):
    """Fetcher over an in-memory {url: (status, body)} map. Unknown URLs 404."""

    def fetch(url: str) -> FakeFetchResult:
        if calls is not None:
            calls.append(url)
        status, body = pages.get(url, (404, ""))
        return FakeFetchResult(
            url=url, final_url=url, status_code=status, headers={}, text=body
        )

    return fetch


def urlset(locs: list[str], lastmod: str | None = "2026-01-01") -> str:
    items = []
    for loc in locs:
        lm = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
        items.append(f"<url><loc>{loc}</loc>{lm}</url>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(items) + "</urlset>"
    )


# ---------------------------------------------------------------------------
# Robots — parser
# ---------------------------------------------------------------------------

class TestRobotsParser:
    def test_empty_body_fixture_does_not_crash(self):
        body = (FIXTURES / "empty_robots.txt").read_text()
        parsed = parse_robots_txt(body)
        assert parsed["empty"] is True
        assert parsed["groups"] == []

    def test_none_body(self):
        parsed = parse_robots_txt(None)
        assert parsed["empty"] is True

    def test_bom_crlf_comments_tolerated(self):
        body = "﻿User-agent: *\r\nDisallow: /private # inline comment\r\n# full comment\r\n"
        parsed = parse_robots_txt(body)
        assert len(parsed["groups"]) == 1
        assert parsed["groups"][0]["rules"] == [("disallow", "/private")]

    def test_group_with_only_crawl_delay_is_committed(self):
        body = "User-agent: GPTBot\nCrawl-delay: 5\n\nUser-agent: *\nDisallow: /\n"
        parsed = parse_robots_txt(body)
        assert len(parsed["groups"]) == 2
        # GPTBot group has zero rules -> allow-all, must NOT fall to wildcard
        groups = find_matching_groups(parsed, "GPTBot")
        assert groups[0]["user_agents"] == ["GPTBot"]
        allowed, _ = evaluate_path_access(groups, "/")
        assert allowed is True

    def test_sitemaps_and_warnings_collected(self):
        body = (
            "User-agent: *\nDisallow:\n"
            "Sitemap: https://example.com/sitemap.xml\n"
            "this line has no colon\n"
            "Unknowndirective: x\n"
        )
        parsed = parse_robots_txt(body)
        assert parsed["sitemaps"] == ["https://example.com/sitemap.xml"]
        assert any("missing colon" in w for w in parsed["parse_warnings"])
        assert any("unknown directive" in w for w in parsed["parse_warnings"])

    def test_rule_before_user_agent_ignored(self):
        parsed = parse_robots_txt("Disallow: /x\nUser-agent: *\nAllow: /\n")
        assert any("before user-agent" in w for w in parsed["parse_warnings"])
        assert parsed["groups"][0]["rules"] == [("allow", "/")]


class TestRobotsUAPrecedence:
    def test_prefix_match_and_longest_token_wins(self):
        body = (
            "User-agent: Googlebot\nDisallow: /\n\n"
            "User-agent: Googlebot-Image\nAllow: /\n\n"
            "User-agent: *\nAllow: /\n"
        )
        parsed = parse_robots_txt(body)
        # 'googlebot' is a prefix of 'Googlebot-Image', but the longer
        # 'googlebot-image' token wins for that crawler.
        img_groups = find_matching_groups(parsed, "Googlebot-Image")
        assert img_groups[0]["user_agents"] == ["Googlebot-Image"]
        # group 'googlebot-image' does NOT match crawler 'Googlebot'
        gbot_groups = find_matching_groups(parsed, "Googlebot")
        assert gbot_groups[0]["user_agents"] == ["Googlebot"]

    def test_wildcard_only_when_no_specific_match(self):
        body = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /admin\n"
        parsed = parse_robots_txt(body)
        gpt_groups = find_matching_groups(parsed, "GPTBot")
        assert gpt_groups[0]["user_agents"] == ["GPTBot"]
        claude_groups = find_matching_groups(parsed, "ClaudeBot")
        assert claude_groups[0]["user_agents"] == ["*"]

    def test_case_insensitive(self):
        parsed = parse_robots_txt("User-agent: gptbot\nDisallow: /\n")
        assert find_matching_groups(parsed, "GPTBot")


class TestRobotsPathEvaluation:
    def test_longest_path_wins(self):
        groups = [{"user_agents": ["*"],
                   "rules": [("disallow", "/a"), ("allow", "/a/b")]}]
        assert evaluate_path_access(groups, "/a/b/c")[0] is True
        assert evaluate_path_access(groups, "/a/x")[0] is False

    def test_allow_wins_tie(self):
        groups = [{"user_agents": ["*"],
                   "rules": [("disallow", "/ab"), ("allow", "/ab")]}]
        assert evaluate_path_access(groups, "/ab")[0] is True

    def test_wildcard_and_dollar_anchor(self):
        groups = [{"user_agents": ["*"], "rules": [("disallow", "/*.pdf$")]}]
        assert evaluate_path_access(groups, "/doc/file.pdf")[0] is False
        assert evaluate_path_access(groups, "/doc/file.pdf?x=1")[0] is True

    def test_empty_disallow_is_allow_all(self):
        groups = [{"user_agents": ["*"], "rules": [("disallow", "")]}]
        allowed, evidence = evaluate_path_access(groups, "/anything")
        assert allowed is True
        assert "empty Disallow" in evidence

    def test_no_groups_permissive(self):
        assert evaluate_path_access([], "/x")[0] is True


class TestInspectRobots:
    def test_reachable_with_rules(self):
        body = "User-agent: *\nDisallow: /admin\nSitemap: https://x.com/sitemap.xml\n"
        out = inspect_robots(body, "https://x.com/page")
        assert out["checks"]["robots_reachable"]["status"] == "pass"
        assert out["checks"]["robots_declares_sitemap"]["status"] == "pass"
        assert out["checks"]["googlebot_allowed"]["status"] == "pass"
        assert out["checks"]["target_path_not_disallowed"]["status"] == "pass"
        assert out["robots_txt"]["sitemaps_declared"] == ["https://x.com/sitemap.xml"]
        assert set(out["bots"]) == set(BOTS_TO_CHECK)
        assert len(out["bots"]) == 16

    def test_unreachable_fixture_expectations(self):
        # Adapted from robots_403_response.json: transport is the caller's
        # job now, so an HTTP 403 arrives here as robots_txt=None.
        expected = json.loads((FIXTURES / "robots_403_response.json").read_text())
        assert expected["http_status"] == 403
        out = inspect_robots(None, "https://example.com/")
        assert out["checks"]["robots_reachable"]["status"] == "fail"
        assert (out["checks"]["target_path_not_disallowed"]["status"]
                == expected["expected_checks"]["target_path_not_disallowed"]["status"])
        assert out["checks"]["robots_declares_sitemap"]["status"] == "na"
        assert out["checks"]["googlebot_allowed"]["status"] == "warn"
        assert out["robots_txt"]["reachable"] is False
        assert out["bots"] == {}

    def test_empty_body_is_warn_permissive(self):
        out = inspect_robots((FIXTURES / "empty_robots.txt").read_text(),
                             "https://example.com/")
        assert out["checks"]["robots_reachable"]["status"] == "warn"
        assert out["checks"]["googlebot_allowed"]["status"] == "pass"
        assert all(b["allowed"] for b in out["bots"].values())

    def test_ai_crawlers_denied(self):
        body = "User-agent: GPTBot\nUser-agent: ClaudeBot\nDisallow: /\n"
        out = inspect_robots(body, "https://x.com/")
        chk = out["checks"]["ai_crawlers_all_allowed"]
        assert chk["status"] == "fail"
        assert "GPTBot" in chk["evidence"] and "ClaudeBot" in chk["evidence"]
        assert out["bots"]["GPTBot"]["allowed"] is False
        assert out["bots"]["Googlebot"]["allowed"] is True
        # target_path_not_disallowed fails because SOME bot is blocked
        assert out["checks"]["target_path_not_disallowed"]["status"] == "fail"

    def test_all_ai_crawlers_explicitly_allowed(self):
        body = "".join(f"User-agent: {b}\n" for b in AI_CRAWLERS_ONLY) + "Allow: /\n"
        out = inspect_robots(body, "https://x.com/")
        assert out["checks"]["ai_crawlers_all_allowed"]["status"] == "pass"

    def test_query_string_matching(self):
        body = "User-agent: *\nDisallow: /*?print=1\n"
        out = inspect_robots(body, "https://x.com/page?print=1")
        assert out["checks"]["googlebot_allowed"]["status"] == "fail"
        out2 = inspect_robots(body, "https://x.com/page")
        assert out2["checks"]["googlebot_allowed"]["status"] == "pass"

    def test_output_json_serializable(self):
        out = inspect_robots("User-agent: *\nDisallow: /a\n", "https://x.com/")
        json.dumps(out)


# ---------------------------------------------------------------------------
# Sitemap
# ---------------------------------------------------------------------------

class TestSitemapEntities:
    """Regression: entity/CDATA sitemap fixture (the original regex-parser bug)."""

    def test_entities_and_cdata_preserved(self):
        xml = (FIXTURES / "sitemap_with_entities.xml").read_text()
        pages = {"https://example.com/sitemap.xml": (200, xml)}
        fetch = make_fake_fetcher(pages)
        out = inspect_sitemap(fetch, "https://example.com/simple-page", None)

        assert out["sitemap"]["found"] is True
        assert out["sitemap"]["discovered_via"] == "default_sitemap_xml"
        assert out["sitemap"]["total_urls_indexed"] == 4
        # &amp; decoded exactly once; CDATA URL intact
        evidence = out["checks"]["sampled_urls_return_200"]["detail"]["sample_results"]
        sampled_urls = {r["url"] for r in evidence}
        assert "https://example.com/search?q=hello&lang=en" in sampled_urls
        assert "https://example.com/articles/what-is-seo&aeo" in sampled_urls
        assert out["checks"]["target_url_in_sitemap"]["status"] == "pass"
        # all 4 have lastmod -> 100% coverage
        assert out["checks"]["lastmod_coverage"]["status"] == "pass"
        json.dumps(out)

    def test_membership_normalization(self):
        xml = (FIXTURES / "sitemap_with_entities.xml").read_text()
        pages = {"https://www.example.com/sitemap.xml": (200, xml)}
        fetch = make_fake_fetcher(pages)
        # www + trailing slash on the target; sitemap has bare host, no slash
        out = inspect_sitemap(fetch, "https://www.example.com/simple-page/", None)
        assert out["checks"]["target_url_in_sitemap"]["status"] == "pass"
        assert "normalized" in out["checks"]["target_url_in_sitemap"]["evidence"]

    def test_normalize_url_for_compare(self):
        a = normalize_url_for_compare("https://www.x.com/a/")
        b = normalize_url_for_compare("http://x.com/a")
        assert a == b == "x.com/a"


class TestSitemapDiscovery:
    def test_robots_directives_win_and_all_are_used(self):
        robots = (
            "User-agent: *\nAllow: /\n"
            "Sitemap: https://x.com/sm-a.xml\n"
            "Sitemap: https://x.com/sm-b.xml\n"
        )
        pages = {
            "https://x.com/sm-a.xml": (200, urlset(["https://x.com/a"])),
            "https://x.com/sm-b.xml": (200, urlset(["https://x.com/b"])),
        }
        calls: list[str] = []
        fetch = make_fake_fetcher(pages, calls)
        out = inspect_sitemap(fetch, "https://x.com/a", robots)
        assert out["sitemap"]["discovered_via"] == "robots_txt_directive"
        assert out["sitemap"]["sitemap_urls"] == [
            "https://x.com/sm-a.xml", "https://x.com/sm-b.xml"
        ]
        assert out["sitemap"]["total_urls_indexed"] == 2
        # robots.txt must NOT be refetched — the body was passed in
        assert not any(u.endswith("/robots.txt") for u in calls)

    def test_fallback_to_sitemap_index_xml(self):
        pages = {
            "https://x.com/sitemap_index.xml": (200, urlset(["https://x.com/a"])),
        }
        fetch = make_fake_fetcher(pages)
        out = inspect_sitemap(fetch, "https://x.com/a", None)
        assert out["sitemap"]["discovered_via"] == "default_sitemap_index_xml"

    def test_not_discovered_all_checks_fail(self):
        fetch = make_fake_fetcher({})
        out = inspect_sitemap(fetch, "https://x.com/a", "User-agent: *\nAllow: /\n")
        assert out["sitemap"]["found"] is False
        assert out["sitemap"]["discovered_via"] == "not_discovered"
        assert len(out["checks"]) == 6
        assert all(c["status"] == "fail" for c in out["checks"].values())

    def test_fetcher_exception_is_contained(self):
        def exploding_fetch(url: str):
            raise ValueError(f"unsafe URL: {url}")

        out = inspect_sitemap(exploding_fetch, "https://x.com/a", None)
        assert out["sitemap"]["found"] is False


class TestSitemapRecursion:
    def test_index_recursion_and_self_reference_loop(self):
        index = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<sitemap><loc>https://x.com/sm-1.xml</loc></sitemap>"
            "<sitemap><loc>https://x.com/sm-2.xml</loc></sitemap>"
            "<sitemap><loc>https://x.com/sitemap.xml</loc></sitemap>"  # self-loop
            "</sitemapindex>"
        )
        pages = {
            "https://x.com/sitemap.xml": (200, index),
            "https://x.com/sm-1.xml": (200, urlset(["https://x.com/p1", "https://x.com/p2"])),
            "https://x.com/sm-2.xml": (200, urlset(["https://x.com/p3"], lastmod=None)),
        }
        fetch = make_fake_fetcher(pages)
        out = inspect_sitemap(fetch, "https://x.com/p1", None)
        assert out["sitemap"]["total_urls_indexed"] == 3
        assert out["sitemap"]["truncated"] is False
        # 2 of 3 have lastmod -> 67% -> warn
        assert out["checks"]["lastmod_coverage"]["status"] == "warn"

    def test_truncation_flag_on_oversized_index(self):
        subs = "".join(
            f"<sitemap><loc>https://x.com/sm-{i}.xml</loc></sitemap>" for i in range(25)
        )
        index = (
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + subs + "</sitemapindex>"
        )
        pages: dict[str, tuple[int, str]] = {"https://x.com/sitemap.xml": (200, index)}
        for i in range(25):
            pages[f"https://x.com/sm-{i}.xml"] = (200, urlset([f"https://x.com/p{i}"]))
        fetch = make_fake_fetcher(pages)
        out = inspect_sitemap(fetch, "https://x.com/not-in-any", None)
        assert out["sitemap"]["truncated"] is True
        assert out["sitemap"]["total_urls_indexed"] == 20  # bounded
        # not found + truncated -> warn, not fail
        assert out["checks"]["target_url_in_sitemap"]["status"] == "warn"

    def test_malformed_child_reported_as_traversal_error(self):
        index = (
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<sitemap><loc>https://x.com/good.xml</loc></sitemap>"
            "<sitemap><loc>https://x.com/bad.xml</loc></sitemap>"
            "</sitemapindex>"
        )
        pages = {
            "https://x.com/sitemap.xml": (200, index),
            "https://x.com/good.xml": (200, urlset(["https://x.com/p1"])),
            "https://x.com/bad.xml": (200, "<urlset><url><loc>broken"),
        }
        fetch = make_fake_fetcher(pages)
        out = inspect_sitemap(fetch, "https://x.com/p1", None)
        assert out["sitemap"]["total_urls_indexed"] == 1
        assert any("parse error" in e for e in out["sitemap"]["traversal_errors"])
        assert out["checks"]["sitemap_reachable"]["status"] == "warn"


class TestSitemapSampling:
    def test_deterministic_md5_sampling(self):
        entries = [{"loc": f"https://x.com/p{i}"} for i in range(40)]
        s1 = deterministic_sample(entries, "https://x.com/target")
        s2 = deterministic_sample(entries, "https://x.com/target")
        assert s1 == s2
        assert len(s1) == 10
        # different seed -> (almost surely) different selection order
        s3 = deterministic_sample(entries, "https://x.com/other-target")
        assert [e["loc"] for e in s3] != [e["loc"] for e in s1]
        # small lists returned whole
        assert deterministic_sample(entries[:3], "https://x.com/t") == entries[:3]

    def test_proportional_grading_dead_and_blocked(self):
        locs = [f"https://x.com/p{i}" for i in range(10)]
        pages: dict[str, tuple[int, str]] = {
            "https://x.com/sitemap.xml": (200, urlset(locs)),
        }
        for loc in locs:
            pages[loc] = (200, "ok")
        # 1 dead of 10 -> 90% reachable -> pass
        pages["https://x.com/p3"] = (500, "")
        fetch = make_fake_fetcher(pages)
        out = inspect_sitemap(fetch, "https://x.com/p0", None)
        chk = out["checks"]["sampled_urls_return_200"]
        assert chk["status"] == "pass"
        assert chk["detail"]["reachable"] == 9
        assert chk["detail"]["dead_urls"] == [("https://x.com/p3", 500)]

        # 2 dead of 10 -> 80% -> warn
        pages["https://x.com/p4"] = (404, "")
        out = inspect_sitemap(make_fake_fetcher(pages), "https://x.com/p0", None)
        assert out["checks"]["sampled_urls_return_200"]["status"] == "warn"

        # blocked-only (403) never fails
        for loc in locs:
            pages[loc] = (403, "")
        out = inspect_sitemap(make_fake_fetcher(pages), "https://x.com/p0", None)
        chk = out["checks"]["sampled_urls_return_200"]
        assert chk["status"] == "warn"
        assert len(chk["detail"]["blocked_urls"]) == 10
        assert chk["detail"]["dead_urls"] == []

    def test_mostly_dead_fails(self):
        locs = [f"https://x.com/p{i}" for i in range(10)]
        pages = {"https://x.com/sitemap.xml": (200, urlset(locs))}
        # every sampled URL 404s (unknown to the fake fetcher)
        fetch = make_fake_fetcher(pages)
        out = inspect_sitemap(fetch, "https://x.com/p0", None)
        assert out["checks"]["sampled_urls_return_200"]["status"] == "fail"


class TestSitemapChecks:
    def test_cross_domain_entries_warn(self):
        locs = ["https://x.com/a", "https://evil.com/b"]
        pages = {
            "https://x.com/sitemap.xml": (200, urlset(locs)),
            "https://x.com/a": (200, "ok"),
            "https://evil.com/b": (200, "ok"),
        }
        out = inspect_sitemap(make_fake_fetcher(pages), "https://x.com/a", None)
        chk = out["checks"]["no_cross_domain_sitemap_entries"]
        assert chk["status"] == "warn"
        assert "https://evil.com/b" in chk["evidence"]

    def test_size_compliance_per_file(self):
        locs = [f"https://x.com/p{i}" for i in range(50_001)]
        pages = {"https://x.com/sitemap.xml": (200, urlset(locs, lastmod=None))}
        out = inspect_sitemap(make_fake_fetcher(pages), "https://x.com/p0", None)
        chk = out["checks"]["sitemap_size_compliance"]
        assert chk["status"] == "warn"
        assert "50,000-URL" in chk["evidence"]

    def test_output_json_serializable(self):
        pages = {"https://x.com/sitemap.xml": (200, urlset(["https://x.com/a"]))}
        out = inspect_sitemap(make_fake_fetcher(pages), "https://x.com/a", None)
        json.dumps(out)


# ---------------------------------------------------------------------------
# Schema markup
# ---------------------------------------------------------------------------

def jsonld_page(*payloads) -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(p)}</script>'
        for p in payloads
    )
    return f"<html><head>{scripts}</head><body><h1>x</h1></body></html>"


class TestSchemaExtraction:
    def test_ssr_landing_fixture(self):
        html = (FIXTURES / "ssr_full_landing.html").read_text()
        out = inspect_schema(html, "https://example.com/")
        summary = out["schema_summary"]
        assert summary["total_blocks"] == 1
        # MedicalBusiness + nested AggregateRating
        assert summary["total_entities"] == 2
        assert summary["entity_types_found"] == ["AggregateRating", "MedicalBusiness"]
        mb = next(v for v in out["validations"] if v["type"] == "MedicalBusiness")
        # fixture has name + @id but no url/address
        assert mb["missing_required"] == ["url"]
        assert mb["missing_google_required"] == ["address"]
        assert mb["has_id"] is True
        assert out["checks"]["no_invalid_entities"]["status"] == "fail"
        assert out["checks"]["schema_entities_present"]["status"] == "pass"
        # AggregateRating has no @id -> partial coverage warn
        assert out["checks"]["schema_id_coverage"]["status"] == "warn"
        json.dumps(out)

    def test_graph_and_list_wrapped_graph_flattened(self):
        block = [{"@graph": [
            {"@type": "Organization", "name": "X", "url": "https://x.com"},
            {"@type": "WebSite", "name": "X", "url": "https://x.com"},
        ]}]
        entities = flatten_entities(extract_schema_blocks(jsonld_page(block)))
        assert {e["@type"] for e in entities} == {"Organization", "WebSite"}

    def test_parse_error_block_reported(self):
        html = ('<script type="application/ld+json">{not json}</script>'
                '<script type="application/ld+json">'
                '{"@type": "Person", "name": "A"}</script>')
        out = inspect_schema(html, "https://x.com/")
        assert out["checks"]["all_schema_blocks_parse"]["status"] == "fail"
        assert out["schema_summary"]["parse_errors"] == 1
        assert out["schema_summary"]["total_entities"] == 1

    def test_no_entities_early_shape(self):
        out = inspect_schema("<html><body>no schema</body></html>", "https://x.com/")
        assert out["schema_summary"] == {"total_entities": 0, "total_blocks": 0}
        assert out["checks"]["schema_entities_present"]["status"] == "fail"
        assert "validations" not in out

    def test_normalize_type_prefers_spec_type(self):
        assert normalize_type(["Physiotherapy", "LocalBusiness"]) == "LocalBusiness"
        assert normalize_type("Product") == "Product"
        assert normalize_type(["CustomThing"]) == "CustomThing"
        assert normalize_type([{"weird": 1}]) == "Unknown"
        assert normalize_type(None) == "Unknown"


class TestSchemaValidation:
    def test_valid_faqpage(self):
        faq = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "@id": "https://x.com/#faq",
            "mainEntity": [
                {"@type": "Question", "name": "Q1?",
                 "acceptedAnswer": {"@type": "Answer", "text": "A1"}},
                {"@type": "Question", "name": "Q2?",
                 "acceptedAnswer": {"@type": "Answer", "text": "A2"}},
            ],
        }
        out = inspect_schema(jsonld_page(faq), "https://x.com/")
        fp = next(v for v in out["validations"] if v["type"] == "FAQPage")
        assert fp["validation_status"] == "valid"
        assert fp["custom_issues"] == []

    def test_faqpage_single_question_dict_is_valid(self):
        faq = {"@type": "FAQPage", "mainEntity": {
            "@type": "Question", "name": "Q?",
            "acceptedAnswer": {"@type": "Answer", "text": "A"}}}
        out = inspect_schema(jsonld_page(faq), "https://x.com/")
        fp = next(v for v in out["validations"] if v["type"] == "FAQPage")
        assert fp["validation_status"] == "valid"

    def test_faqpage_false_positive_wrong_types(self):
        # "FAQ-shaped" markup whose mainEntity items are not Questions —
        # the false-positive class the source fixtures guard against.
        fake_faq = {"@type": "FAQPage", "mainEntity": [
            {"@type": "WebPageElement", "name": "United Arab Emirates"},
            {"@type": "WebPageElement", "name": "Saudi Arabia"},
        ]}
        out = inspect_schema(jsonld_page(fake_faq), "https://x.com/")
        fp = next(v for v in out["validations"] if v["type"] == "FAQPage")
        assert fp["validation_status"] == "invalid"
        assert any("expected Question" in c for c in fp["custom_issues"])

    def test_faqpage_missing_answer_text(self):
        faq = {"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": "Q?",
             "acceptedAnswer": {"@type": "Answer"}}]}
        out = inspect_schema(jsonld_page(faq), "https://x.com/")
        fp = next(v for v in out["validations"] if v["type"] == "FAQPage")
        assert any("missing text" in c for c in fp["custom_issues"])

    def test_faqpage_empty_mainentity(self):
        out = inspect_schema(jsonld_page({"@type": "FAQPage", "mainEntity": []}),
                             "https://x.com/")
        fp = next(v for v in out["validations"] if v["type"] == "FAQPage")
        assert fp["validation_status"] == "invalid"
        # empty list also fails required-field presence
        assert fp["missing_required"] == ["mainEntity"]
        assert "mainEntity is empty" in fp["custom_issues"]

    def test_breadcrumb_positions(self):
        bad = {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home"},
            {"@type": "ListItem", "position": 3, "name": "Blog"},
        ]}
        out = inspect_schema(jsonld_page(bad), "https://x.com/")
        bl = next(v for v in out["validations"] if v["type"] == "BreadcrumbList")
        assert bl["validation_status"] == "invalid"
        assert any("position=3, expected 2" in c for c in bl["custom_issues"])

        good = {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": "1", "name": "Home"},  # string coerced
            {"@type": "ListItem", "position": 2, "name": "Blog"},
        ]}
        out = inspect_schema(jsonld_page(good), "https://x.com/")
        bl = next(v for v in out["validations"] if v["type"] == "BreadcrumbList")
        assert bl["custom_issues"] == []

    def test_product_missing_offers_is_invalid(self):
        product = {"@type": "Product", "name": "Widget"}
        out = inspect_schema(jsonld_page(product), "https://x.com/")
        pv = next(v for v in out["validations"] if v["type"] == "Product")
        assert pv["validation_status"] == "invalid"
        assert pv["missing_google_required"] == ["offers"]

    def test_empty_values_count_as_missing(self):
        org = {"@type": "Organization", "name": "  ", "url": "", "sameAs": []}
        out = inspect_schema(jsonld_page(org), "https://x.com/")
        ov = next(v for v in out["validations"] if v["type"] == "Organization")
        assert set(ov["missing_required"]) == {"name", "url"}

    def test_unknown_type_reported(self):
        out = inspect_schema(jsonld_page({"@type": "Physiotherapy", "name": "X"}),
                             "https://x.com/")
        assert out["checks"]["known_schema_types"]["status"] == "warn"
        assert out["schema_summary"]["unknown_types"] == 1

    def test_field_specs_registry_complete(self):
        # Fable's registry ships 37 @type specs (the contract's "28" counts
        # only the top-level docstring list); every spec has the 3 tiers.
        assert len(FIELD_SPECS) == 37
        for spec in FIELD_SPECS.values():
            assert set(spec) >= {"required", "google_required", "recommended"}

    def test_deterministic(self):
        html = (FIXTURES / "ssr_full_landing.html").read_text()
        assert (inspect_schema(html, "https://x.com/")
                == inspect_schema(html, "https://x.com/"))
