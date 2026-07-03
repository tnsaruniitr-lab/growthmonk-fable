"""Tests for gm.audit.safety / fetch / bev — no network, no live DNS.

All HTTP goes through httpx.MockTransport; all DNS goes through a
monkeypatched gm.audit.safety._getaddrinfo.
"""

import socket
from pathlib import Path

import httpx
import pytest

from gm.audit import safety
from gm.audit.bev import (
    NOT_FOUND_PATH,
    USER_AGENTS,
    bots_eye_view,
    classify_ssr,
    detect_spa_signals,
    extract_first_h1,
    extract_title,
    faq_schema_count,
    faq_visible_count,
    looks_like_question,
    visible_text,
    visible_word_count,
)
from gm.audit.fetch import make_fetcher
from gm.audit.safety import UnsafeURL, check_url_safe, validate_url

FIXTURES = Path(__file__).parent / "fixtures" / "bev"

SSR_LANDING = (FIXTURES / "ssr_full_landing.html").read_text()
SPA_SHELL = (FIXTURES / "spa_shell_same_as_404.html").read_text()
COUNTRY_ACCORDION = (FIXTURES / "country_accordion_not_faq.html").read_text()
REAL_FAQ = (FIXTURES / "real_faq_accordion.html").read_text()

NOT_FOUND_PAGE = "<html><head><title>404</title></head><body><h1>Page not found</h1></body></html>"
THIN_PAGE = (
    "<html><head><title>Thin</title></head><body><h1>Hi there</h1><p>Short.</p></body></html>"
)


def _resolver(*addrs):
    """Fake getaddrinfo returning the given IPs (default: one public IP)."""
    ips = addrs or ("93.184.216.34",)

    def resolve(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    return resolve


@pytest.fixture
def public_dns(monkeypatch):
    """Every hostname resolves to a public IP — no live DNS in any test."""
    monkeypatch.setattr(safety, "_getaddrinfo", _resolver())


# ----------------------------------------------------------------------
# safety.py
# ----------------------------------------------------------------------

class TestSafety:
    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/file",
            "file:///etc/passwd",
            "gopher://example.com",
            "example.com",  # no scheme
            "",
        ],
    )
    def test_non_http_schemes_rejected(self, url):
        ok, reason = check_url_safe(url)
        assert not ok
        with pytest.raises(UnsafeURL):
            validate_url(url)

    def test_credentials_rejected(self):
        ok, reason = check_url_safe("https://user:pass@example.com/")
        assert not ok
        assert "credentials" in reason

    @pytest.mark.parametrize("host", ["localhost", "metadata.google.internal", "metadata"])
    def test_blocked_hostnames(self, host):
        with pytest.raises(UnsafeURL):
            validate_url(f"http://{host}/x")

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/",
            "http://10.1.2.3/",
            "http://192.168.0.5/admin",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data/",  # AWS/GCP/Azure IMDS
            "http://100.100.100.200/",  # Alibaba metadata
            "http://0.0.0.0/",
            "http://224.0.0.1/",  # multicast
            "http://[::1]/",
            "http://[fd00:ec2::254]/",  # AWS IMDSv6
            "http://[::ffff:169.254.169.254]/",  # IPv4-mapped IPv6 metadata
            "http://[::ffff:10.0.0.1]/",  # IPv4-mapped IPv6 private
        ],
    )
    def test_disallowed_literal_ips(self, url):
        ok, reason = check_url_safe(url)
        assert not ok, url
        with pytest.raises(UnsafeURL):
            validate_url(url)

    def test_public_literal_ip_allowed(self):
        assert validate_url("http://93.184.216.34/") == "http://93.184.216.34/"

    def test_hostname_resolving_public_is_allowed(self, monkeypatch):
        monkeypatch.setattr(safety, "_getaddrinfo", _resolver("93.184.216.34"))
        assert validate_url("https://example.com/page") == "https://example.com/page"

    def test_hostname_resolving_private_is_blocked(self, monkeypatch):
        monkeypatch.setattr(safety, "_getaddrinfo", _resolver("10.0.0.5"))
        ok, reason = check_url_safe("https://internal.example.com/")
        assert not ok
        assert "disallowed address" in reason

    def test_any_resolved_address_private_blocks(self, monkeypatch):
        # DNS-rebinding style: one public + one private A record -> blocked.
        monkeypatch.setattr(safety, "_getaddrinfo", _resolver("93.184.216.34", "192.168.1.1"))
        ok, _ = check_url_safe("https://rebind.example.com/")
        assert not ok

    def test_dns_failure_blocks(self, monkeypatch):
        def boom(host, port):
            raise socket.gaierror(8, "nodename nor servname provided")

        monkeypatch.setattr(safety, "_getaddrinfo", boom)
        ok, reason = check_url_safe("https://doesnotexist.example/")
        assert not ok
        assert "dns resolution failed" in reason

    def test_resolve_false_skips_dns(self, monkeypatch):
        def boom(host, port):
            raise AssertionError("resolver must not be called")

        monkeypatch.setattr(safety, "_getaddrinfo", boom)
        ok, reason = check_url_safe("https://example.com/", resolve=False)
        assert ok and reason is None

    def test_non_string_rejected(self):
        ok, _ = check_url_safe(None)  # type: ignore[arg-type]
        assert not ok


# ----------------------------------------------------------------------
# fetch.py
# ----------------------------------------------------------------------

class TestFetch:
    def _fetcher(self, handler, user_agent="TestUA/1.0"):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return make_fetcher(client=client, user_agent=user_agent)

    def test_simple_200(self, public_dns):
        seen = {}

        def handler(request):
            seen["ua"] = request.headers["user-agent"]
            return httpx.Response(200, text="hello", headers={"content-type": "text/html"})

        fetch = self._fetcher(handler)
        r = fetch("https://example.com/")
        assert r.status_code == 200
        assert r.text == "hello"
        assert r.final_url == "https://example.com/"
        assert r.redirect_chain == ["https://example.com/"]
        assert r.headers["content-type"] == "text/html"
        assert r.elapsed_ms >= 0
        assert seen["ua"] == "TestUA/1.0"

    def test_redirects_followed_with_relative_location(self, public_dns):
        def handler(request):
            if request.url.path == "/a":
                return httpx.Response(301, headers={"location": "/b"})
            return httpx.Response(200, text="landed")

        r = self._fetcher(handler)("https://example.com/a")
        assert r.status_code == 200
        assert r.final_url == "https://example.com/b"
        assert r.redirect_chain == ["https://example.com/a", "https://example.com/b"]

    def test_max_five_redirects_returns_final_3xx(self, public_dns):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            n = len(calls)
            return httpx.Response(301, headers={"location": f"/hop{n}"})

        r = self._fetcher(handler)("https://example.com/start")
        assert r.status_code == 301  # unresolved after the cap; BEV grades it inconclusive
        assert len(calls) == 6  # initial request + 5 followed redirects
        assert len(r.redirect_chain) == 6

    def test_redirect_to_metadata_ip_raises(self, public_dns):
        def handler(request):
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})

        with pytest.raises(UnsafeURL):
            self._fetcher(handler)("https://example.com/")

    def test_redirect_to_private_hostname_raises(self, monkeypatch):
        def resolve(host, port):
            ip = "93.184.216.34" if host == "example.com" else "10.0.0.9"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

        monkeypatch.setattr(safety, "_getaddrinfo", resolve)

        def handler(request):
            return httpx.Response(302, headers={"location": "https://internal.corp/"})

        with pytest.raises(UnsafeURL):
            self._fetcher(handler)("https://example.com/")

    def test_initial_url_validated_before_any_request(self):
        def handler(request):
            raise AssertionError("transport must not be reached")

        with pytest.raises(UnsafeURL):
            self._fetcher(handler)("http://localhost/")


# ----------------------------------------------------------------------
# bev.py — analyzer units
# ----------------------------------------------------------------------

class TestFaqDetection:
    def test_country_accordion_is_not_faq(self):
        assert faq_visible_count(COUNTRY_ACCORDION) == (0, "none_detected")

    def test_real_faq_accordion_counts_six(self):
        assert faq_visible_count(REAL_FAQ) == (6, "details_summary_question")

    def test_ssr_landing_counts_six(self):
        count, method = faq_visible_count(SSR_LANDING)
        assert count == 6
        assert method == "details_summary_question"

    def test_german_question_words(self):
        assert looks_like_question("Wie funktioniert die Abrechnung")
        assert looks_like_question("Kann ich kurzfristig stornieren")
        assert not looks_like_question("Kingdom of Saudi Arabia")

    def test_faq_schema_count_with_graph(self):
        html = """<script type="application/ld+json">
        {"@context":"https://schema.org","@graph":[{"@type":"FAQPage","mainEntity":[
          {"@type":"Question","name":"How fast?"},{"@type":"Question","name":"How much?"}
        ]}]}</script>"""
        assert faq_schema_count(html) == 2

    def test_faq_schema_single_mainentity_dict_counts_one(self):
        html = """<script type='application/ld+json'>
        {"@type":["WebPage","FAQPage"],"mainEntity":{"@type":"Question","name":"Why?"}}
        </script>"""
        assert faq_schema_count(html) == 1


class TestVisibleText:
    def test_script_style_head_skipped(self):
        html = (
            "<html><head><title>T</title><style>.x{color:red}</style></head>"
            "<body><script>var a=1;</script><p>real words here</p></body></html>"
        )
        assert visible_text(html) == "real words here"
        assert visible_word_count(html) == 3

    def test_fixture_word_counts(self):
        assert visible_word_count(SSR_LANDING) > 500
        assert visible_word_count(SPA_SHELL) == 0

    def test_title_and_h1_extraction(self):
        assert extract_title(SSR_LANDING).startswith("At-Home Blood Tests in Dubai")
        assert extract_first_h1(SSR_LANDING) == (
            "At-Home Healthcare Across UAE, KSA, Qatar, and Kuwait"
        )

    def test_spa_signals(self):
        assert "angular_app_root" in detect_spa_signals(SPA_SHELL)
        assert detect_spa_signals(SSR_LANDING) == []


class TestClassifySsr:
    def test_transport_gate(self):
        assert classify_ssr(0, False, [], http_code=0) == "fetch_failed"
        assert classify_ssr(0, False, [], http_code=301) == "unresolved_redirect"
        for code in (401, 403, 429):
            assert classify_ssr(500, False, [], http_code=code) == "bot_blocked"
        assert classify_ssr(500, False, [], http_code=500) == "http_error"
        assert classify_ssr(500, False, [], http_code=404) == "http_error"

    def test_same_as_404_wins_over_word_count(self):
        assert classify_ssr(900, True, [], http_code=200) == "spa_no_ssr"

    def test_word_count_thresholds(self):
        assert classify_ssr(199, False, [], http_code=200) == "minimal_content"
        assert classify_ssr(199, False, ["nextjs"], http_code=200) == "js_dependent"
        assert classify_ssr(200, False, [], http_code=200) == "partial_ssr"
        assert classify_ssr(499, False, [], http_code=200) == "partial_ssr"
        assert classify_ssr(500, False, [], http_code=200) == "fully_accessible"

    def test_ssr_shell_js_hidden_content(self):
        snippet = "<h1>Select Language</h1>" + "self.__next_f.push([1])" + "x" * 41_000
        assert (
            classify_ssr(50, False, ["nextjs"], h1_first="Select Language",
                         html_snippet=snippet, http_code=200)
            == "ssr_shell_js_hidden_content"
        )


# ----------------------------------------------------------------------
# bev.py — end-to-end probe orchestration (fake transports, zero network)
# ----------------------------------------------------------------------

def factory_from_handler(handler):
    """fetcher_factory: UA string -> Fetcher backed by one MockTransport handler."""

    def factory(ua: str):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return make_fetcher(client=client, user_agent=ua)

    return factory


class TestBotsEyeView:
    def test_fully_accessible_site(self, public_dns):
        def handler(request):
            if request.url.path == NOT_FOUND_PATH:
                return httpx.Response(404, text=NOT_FOUND_PAGE)
            return httpx.Response(200, text=SSR_LANDING)

        r = bots_eye_view("https://example.com/", factory_from_handler(handler))
        assert r.classification == "fully_accessible"
        assert r.spa_shell is False
        assert r.cloaking_suspected is False
        assert set(USER_AGENTS) <= set(r.per_ua)
        d = r.per_ua["default"]
        assert d["status"] == 200
        assert d["blocked"] is False
        assert d["bytes"] > 0
        assert d["title"].startswith("At-Home Blood Tests")
        assert d["h1"].startswith("At-Home Healthcare")
        assert r.per_ua["gptbot"]["status"] == 200

    def test_scheme_defaulted_to_https(self, public_dns):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, text=SSR_LANDING)

        bots_eye_view("example.com", factory_from_handler(handler))
        assert all(u.startswith("https://example.com") for u in seen)

    def test_unsafe_target_raises_before_probing(self):
        def handler(request):
            raise AssertionError("must not fetch")

        with pytest.raises(UnsafeURL):
            bots_eye_view("http://169.254.169.254/latest/", factory_from_handler(handler))

    def test_spa_shell_same_as_404(self, public_dns):
        def handler(request):
            return httpx.Response(200, text=SPA_SHELL)  # identical shell for every URL

        r = bots_eye_view("https://spa.example.com/pricing", factory_from_handler(handler))
        assert r.classification == "spa_no_ssr"
        assert r.spa_shell is True
        assert any("identical shell" in n for n in r.notes)

    def test_soft_404_redirect_is_not_spa_shell(self, public_dns):
        def handler(request):
            if request.url.path == NOT_FOUND_PATH:
                return httpx.Response(302, headers={"location": "/"})
            return httpx.Response(200, text=SSR_LANDING)

        r = bots_eye_view("https://example.com/", factory_from_handler(handler))
        assert r.spa_shell is False
        assert r.classification == "fully_accessible"
        assert any("soft-404" in n for n in r.notes)

    def test_cloaking_thin_page_for_gptbot(self, public_dns):
        def handler(request):
            if request.url.path == NOT_FOUND_PATH:
                return httpx.Response(404, text=NOT_FOUND_PAGE)
            if "GPTBot" in request.headers["user-agent"]:
                return httpx.Response(200, text=THIN_PAGE)
            return httpx.Response(200, text=SSR_LANDING)

        r = bots_eye_view("https://example.com/", factory_from_handler(handler))
        assert r.cloaking_suspected is True
        assert r.per_ua["gptbot"].get("cloaking_flagged") is True
        assert "delta_vs_default" in r.per_ua["gptbot"]
        assert any("Cloaking suspected" in n for n in r.notes)

    def test_bot_blocking_is_not_cloaking(self, public_dns):
        def handler(request):
            if request.url.path == NOT_FOUND_PATH:
                return httpx.Response(404, text=NOT_FOUND_PAGE)
            ua = request.headers["user-agent"]
            if "GPTBot" in ua or "ClaudeBot" in ua:
                return httpx.Response(403, text="Forbidden")
            return httpx.Response(200, text=SSR_LANDING)

        r = bots_eye_view("https://example.com/", factory_from_handler(handler))
        assert r.classification == "fully_accessible"
        assert r.cloaking_suspected is False  # 403 error page must not read as cloaking
        assert r.per_ua["gptbot"]["blocked"] is True
        assert r.per_ua["claudebot"]["blocked"] is True
        assert r.per_ua["googlebot"]["blocked"] is False
        note = next(n for n in r.notes if "blocked while browser UA succeeds" in n)
        assert "gptbot=403" in note and "claudebot=403" in note

    def test_unresolved_redirect_is_inconclusive(self, public_dns):
        def handler(request):
            return httpx.Response(301, headers={"location": "/next"})

        r = bots_eye_view("https://example.com/", factory_from_handler(handler))
        assert r.classification == "unresolved_redirect"
        assert any("Probe inconclusive" in n and "redirect" in n for n in r.notes)

    def test_fetch_failed(self, public_dns):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        r = bots_eye_view("https://down.example.com/", factory_from_handler(handler))
        assert r.classification == "fetch_failed"
        assert r.per_ua["default"]["status"] == 0
        assert "error" in r.per_ua["default"]
        assert any("Probe inconclusive: fetch failed" in n for n in r.notes)

    def test_browser_blocked_but_bots_ok_note(self, public_dns):
        def handler(request):
            if request.url.path == NOT_FOUND_PATH:
                return httpx.Response(404, text=NOT_FOUND_PAGE)
            ua = request.headers["user-agent"]
            if "Googlebot" in ua or "GPTBot" in ua or "Perplexity" in ua or "ClaudeBot" in ua:
                return httpx.Response(200, text=SSR_LANDING)
            return httpx.Response(403, text="Forbidden")

        r = bots_eye_view("https://example.com/", factory_from_handler(handler))
        assert r.classification == "bot_blocked"  # reflects the default/browser probe
        assert any("fetched the page successfully (2xx)" in n for n in r.notes)

    def test_faq_mismatch_note(self, public_dns):
        page = (
            "<html><head><title>F</title>"
            '<script type="application/ld+json">{"@type":"FAQPage","mainEntity":['
            '{"@type":"Question","name":"Is this question visible anywhere?"},'
            '{"@type":"Question","name":"What about this hidden one?"}]}</script>'
            "</head><body><h1>No FAQ text on page</h1>"
            "<p>" + "word " * 600 + "</p></body></html>"
        )

        def handler(request):
            if request.url.path == NOT_FOUND_PATH:
                return httpx.Response(404, text=NOT_FOUND_PAGE)
            return httpx.Response(200, text=page)

        r = bots_eye_view("https://example.com/", factory_from_handler(handler))
        assert r.per_ua["default"]["faq_integrity"] == "mismatch"
        assert any("FAQ schema/HTML mismatch" in n for n in r.notes)

    def test_ua_strings_are_fables(self):
        assert "Googlebot/2.1" in USER_AGENTS["googlebot"]
        assert "GPTBot/1.0" in USER_AGENTS["gptbot"]
        assert "PerplexityBot/1.0" in USER_AGENTS["perplexitybot"]
        assert "ClaudeBot/1.0" in USER_AGENTS["claudebot"]
        assert USER_AGENTS["default"].startswith("Mozilla/5.0 (Macintosh")
