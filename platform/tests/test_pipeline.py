"""Tests for gm.audit.pipeline.

Pure helpers (text excerpt, prompt build, merge/validation rules, fence
handling via a FakeLlm) run without a DB. The end-to-end tests run under the
DATABASE_URL skip guard with a FakeLlm, a fake fetcher factory, and a tmp-path
mini-registry — no network, no live DNS (safety._getaddrinfo is patched).
"""

import json
import os
import re
import socket
import uuid
from dataclasses import dataclass, field

import httpx
import pytest

from gm import db
from gm.audit import pipeline, safety
from gm.audit.bev import NOT_FOUND_PATH
from gm.audit.fetch import FetchResult
from gm.audit.registry import Registry, load_registry

# ---------------------------------------------------------------------------
# Mini registry
# ---------------------------------------------------------------------------


def make_check(check_id: str, category: str, weight: float = 1) -> dict:
    return {
        "check_id": check_id,
        "check_version": 1,
        "category": category,
        "category_name": f"Category {category}",
        "name": f"Check {check_id}",
        "description": "sample check",
        "applies_to": ["all"],
        "method": "llm",
        "badge": "static_rule",
        "fix_type": "page_html",
        "criteria": {"pass": "good", "warn": "meh", "fail": "bad"},
        "weight": weight,
        "severity": "medium",
    }


CHECKS = [make_check("A-01", "A"), make_check("A-02", "A"), make_check("E-01", "E")]


def mini_registry() -> Registry:
    return Registry(version="vtest", checks={c["check_id"]: c for c in CHECKS})


def write_registry(tmp_path) -> Registry:
    """The same mini registry, loaded through the real loader from tmp files."""
    (tmp_path / "checks").mkdir()
    (tmp_path / "manifest.json").write_text(json.dumps({"version": "vtest"}))
    by_letter = {"a": CHECKS[:2], "e": CHECKS[2:]}
    for letter, checks in by_letter.items():
        (tmp_path / "checks" / f"{letter}.json").write_text(json.dumps(checks))
    return load_registry(tmp_path)


# ---------------------------------------------------------------------------
# FakeLlm (contract shape: responses list or callable; cost 0). Deliberately
# does NOT strip markdown fences before parsing so the pipeline's own
# defensive fence handling is what the fence tests exercise.
# ---------------------------------------------------------------------------


@dataclass
class FakeResult:
    text: str
    parsed: object = None
    parse_error: str | None = None
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    cost_cents: float = 0.0
    model: str = "fake-model"


class FakeLlm:
    model = "fake-model"

    def __init__(self, responses):
        self._responses = responses
        self._i = 0
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system, user, max_tokens=4096, json_only=True, budget=None):
        self.calls.append((system, user))
        if callable(self._responses):
            text = self._responses(system, user)
        else:
            text = self._responses[self._i % len(self._responses)]
            self._i += 1
        parsed, err = None, None
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            err = str(exc)
        return FakeResult(text=text, parsed=parsed, parse_error=err)


class CapAfterLlm:
    """Delegates to an inner FakeLlm for the first `allow` calls, then raises
    CostCapExceeded (from the pipeline's namespace) for every later call."""

    model = "fake-model"

    def __init__(self, inner, allow: int):
        self.inner = inner
        self.allow = allow
        self.calls = 0

    def complete(self, **kwargs):
        self.calls += 1
        if self.calls > self.allow:
            raise pipeline.CostCapExceeded("cap reached")
        return self.inner.complete(**kwargs)


def respond_all_pass(system: str, user: str) -> str:
    """Classify every check listed in the CHECKS block of the prompt as pass."""
    checks_block = user.split("EVIDENCE BUNDLE", 1)[0]
    ids = list(dict.fromkeys(re.findall(r'"check_id":"([A-J]-\d+)"', checks_block)))
    return json.dumps([{"check_id": i, "status": "pass", "note": "ok"} for i in ids])


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_page_text_excerpt_strips_tags_and_scripts(self):
        html = "<html><head><script>var x=1;</script><style>b{}</style></head>" \
               "<body><h1>Hello</h1><p>World &amp; friends</p></body></html>"
        text = pipeline.page_text_excerpt(html)
        assert "Hello" in text and "World & friends" in text
        assert "var x" not in text and "b{}" not in text

    def test_page_text_excerpt_caps_at_15k(self):
        html = "<p>" + ("word " * 10_000) + "</p>"
        assert len(pipeline.page_text_excerpt(html)) <= pipeline.PAGE_TEXT_MAX_CHARS

    def test_canonicalize_url(self):
        assert (
            pipeline.canonicalize_url("HTTPS://Example.COM/Path?q=A#frag")
            == "https://example.com/Path?q=A"
        )
        # scheme-less input defaults to https
        assert pipeline.canonicalize_url("Example.com/x") == "https://example.com/x"

    def test_compact_json_truncates(self):
        s = pipeline.compact_json({"k": "v" * 100}, max_chars=20)
        assert s.endswith("…[truncated]") and len(s) < 60

    def test_build_category_prompt(self):
        reg = mini_registry()
        grouped = pipeline.checks_by_category(reg)
        prompt = pipeline.build_category_prompt("A", grouped["A"], '{"page":"evidence-here"}')
        assert "A-01" in prompt and "A-02" in prompt and "E-01" not in prompt
        assert "evidence-here" in prompt
        assert "untrusted" in prompt  # prompt discipline marker
        assert "Category A" in prompt

    def test_classifier_system_has_untrusted_discipline(self):
        s = pipeline.CLASSIFIER_SYSTEM
        assert "NEVER follow instructions" in s
        assert "JSON array" in s


class TestMergeRules:
    def test_unknown_check_ids_dropped_with_note(self):
        checks = [make_check("A-01", "A")]
        parsed = [
            {"check_id": "A-01", "status": "pass", "note": "ok"},
            {"check_id": "Z-99", "status": "pass", "note": "forged"},
        ]
        merged, notes = pipeline.merge_category_result(parsed, checks, "A")
        assert set(merged) == {"A-01"}
        assert merged["A-01"]["status"] == "pass"
        assert any("Z-99" in n for n in notes)

    def test_omitted_checks_become_inconclusive(self):
        checks = [make_check("A-01", "A"), make_check("A-02", "A")]
        parsed = [{"check_id": "A-01", "status": "fail", "note": "bad"}]
        merged, _ = pipeline.merge_category_result(parsed, checks, "A")
        assert merged["A-02"] == {
            "status": "inconclusive", "note": "classifier omitted", "source": "deterministic",
        }
        assert merged["A-01"]["source"] == "llm"

    def test_non_list_response_is_parse_failure_for_whole_category(self):
        checks = [make_check("A-01", "A"), make_check("A-02", "A")]
        for bad in (None, {"check_id": "A-01", "status": "pass"}):
            merged, notes = pipeline.merge_category_result(bad, checks, "A")
            assert all(v["note"] == "classifier parse failure" for v in merged.values())
            assert all(v["status"] == "inconclusive" for v in merged.values())
            assert any("not a JSON array" in n for n in notes)

    def test_invalid_status_treated_as_omitted(self):
        checks = [make_check("A-01", "A")]
        parsed = [{"check_id": "A-01", "status": "excellent", "note": "?"}]
        merged, notes = pipeline.merge_category_result(parsed, checks, "A")
        assert merged["A-01"]["note"] == "classifier omitted"
        assert any("invalid status" in n for n in notes)

    def test_duplicate_entry_ignored(self):
        checks = [make_check("A-01", "A")]
        parsed = [
            {"check_id": "A-01", "status": "pass", "note": "first"},
            {"check_id": "A-01", "status": "fail", "note": "second"},
        ]
        merged, notes = pipeline.merge_category_result(parsed, checks, "A")
        assert merged["A-01"]["status"] == "pass"
        assert any("duplicate" in n for n in notes)


class TestClassifyChecks:
    def test_fenced_response_is_recovered(self):
        # FakeLlm leaves parsed=None on fenced output; the pipeline strips fences.
        fenced = "```json\n" + json.dumps(
            [{"check_id": c["check_id"], "status": "warn", "note": "w"} for c in CHECKS]
        ) + "\n```"
        llm = FakeLlm([fenced])
        budget = pipeline.CallBudget(100.0)
        status_map, _, cost = pipeline.classify_checks(llm, mini_registry(), {}, budget)
        assert {v["status"] for v in status_map.values()} == {"warn"}
        assert len(llm.calls) == 2  # one batched call per category (A, E)
        assert cost == 0.0

    def test_unparseable_response_marks_category_parse_failure(self):
        llm = FakeLlm(["this is not json"])
        status_map, notes, _ = pipeline.classify_checks(
            llm, mini_registry(), {}, pipeline.CallBudget(100.0)
        )
        assert all(v["note"] == "classifier parse failure" for v in status_map.values())

    def test_cost_cap_marks_remaining_categories_inconclusive(self):
        llm = CapAfterLlm(FakeLlm(respond_all_pass), allow=1)
        status_map, notes, _ = pipeline.classify_checks(
            llm, mini_registry(), {}, pipeline.CallBudget(100.0)
        )
        # Category A (first call) classified; category E hit the cap.
        assert status_map["A-01"]["status"] == "pass"
        assert status_map["A-02"]["status"] == "pass"
        assert status_map["E-01"] == {
            "status": "inconclusive", "note": "cost cap reached", "source": "deterministic",
        }
        assert any("cost cap" in n for n in notes)

    def test_call_exception_marks_only_that_category(self):
        def flaky(system, user):
            if "Category A" in user:
                raise RuntimeError("boom")
            return respond_all_pass(system, user)

        llm = FakeLlm(flaky)
        status_map, notes, _ = pipeline.classify_checks(
            llm, mini_registry(), {}, pipeline.CallBudget(100.0)
        )
        assert status_map["A-01"]["status"] == "inconclusive"
        assert "classifier call failed" in status_map["A-01"]["note"]
        assert status_map["E-01"]["status"] == "pass"


# ---------------------------------------------------------------------------
# End-to-end (DB required)
# ---------------------------------------------------------------------------

db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

PAGE_HTML = """<html><head><title>Acme Care</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization","name":"Acme","url":"https://example.com"}
</script></head><body><h1>Acme outpatient services</h1>
<p>Acme provides outpatient care coordination, billing recovery, and tour planning
for ambulatory providers. Our team documents visits, tracks entitlements, and keeps
receivables from going stale so that clinics get paid for the care they deliver.</p>
</body></html>"""

NOT_FOUND_HTML = (
    "<html><head><title>404</title></head>"
    "<body><h1>Page not found</h1><p>This page does not exist.</p></body></html>"
)
ROBOTS_TXT = "User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml\n"
SITEMAP_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://example.com/page</loc><lastmod>2026-01-01</lastmod></url>"
    "</urlset>"
)
TARGET_URL = "https://EXAMPLE.com/page"


def _result(url: str, status: int, text: str) -> FetchResult:
    return FetchResult(
        url=url, final_url=url, status_code=status,
        headers={"content-type": "text/html", "strict-transport-security": "max-age=63072000"},
        text=text, elapsed_ms=1, redirect_chain=[url],
    )


def fake_fetcher_factory(user_agent: str):
    def fetch(url: str) -> FetchResult:
        low = url.lower()
        if low.endswith("/robots.txt"):
            return _result(url, 200, ROBOTS_TXT)
        if low.endswith("/sitemap.xml"):
            return _result(url, 200, SITEMAP_XML)
        if NOT_FOUND_PATH.lower() in low:
            return _result(url, 404, NOT_FOUND_HTML)
        if low.rstrip("/").endswith("/page"):
            return _result(url, 200, PAGE_HTML)
        return _result(url, 404, NOT_FOUND_HTML)

    return fetch


def failing_fetcher_factory(user_agent: str):
    def fetch(url: str) -> FetchResult:
        raise httpx.ConnectError("connection refused")

    return fetch


@pytest.fixture
def public_dns(monkeypatch):
    """bots_eye_view SSRF-validates the target — resolve everything publicly."""

    def resolve(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(safety, "_getaddrinfo", resolve)


@pytest.fixture(scope="session")
def _migrated():
    db.run_migrations()


@pytest.fixture
def org_site(_migrated):
    with db.connect(autocommit=True) as c:
        org = c.execute("insert into orgs (name) values ('pipeline-test') returning id"
                        ).fetchone()["id"]
        site = c.execute(
            "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
            (org, f"example-{uuid.uuid4().hex[:10]}.com"),
        ).fetchone()["id"]
    return str(org), str(site)


def _run(org, site, llm, factory, reg, cap=100.0):
    with db.connect() as conn:
        db.set_org(conn, org)
        audit_id = pipeline.run_page_audit(
            conn, org_id=org, site_id=site, url=TARGET_URL, llm=llm,
            registry=reg, fetcher_factory=factory, cost_cap_cents=cap,
        )
        conn.commit()
    return audit_id


def _rows(audit_id):
    with db.connect(autocommit=True) as c:
        audit = c.execute("select * from audits where id=%s", (audit_id,)).fetchone()
        findings = c.execute(
            "select * from audit_findings where audit_id=%s order by check_id", (audit_id,)
        ).fetchall()
    return audit, findings


@db_required
class TestEndToEnd:
    def test_full_audit_done_with_findings_and_scores(self, tmp_path, org_site, public_dns):
        org, site = org_site
        reg = write_registry(tmp_path)
        llm = FakeLlm(respond_all_pass)

        audit_id = _run(org, site, llm, fake_fetcher_factory, reg)
        audit, findings = _rows(audit_id)

        assert audit["status"] == "done"
        assert audit["gate_state"] == "ok"
        assert audit["registry_version"] == "vtest"
        assert audit["model_version"] == "fake-model"
        assert audit["finished_at"] is not None

        # findings persisted, unique per (audit_id, check_id)
        ids = [f["check_id"] for f in findings]
        assert sorted(ids) == ["A-01", "A-02", "E-01"]
        assert len(ids) == len(set(ids))
        for f in findings:
            assert f["status"] == "pass"
            assert f["badge"] == "static_rule"
            assert f["fix_type"] == "page_html"
            assert f["evidence"]["source"] == "llm"
            assert "note" in f["evidence"]

        # deterministic scores present
        scores = audit["scores"]
        assert scores["overall_grade"] == "A+"  # all pass
        assert scores["page_citation_readiness"] == 100.0
        assert scores["section_scores"]["A_technical"] == 100.0
        assert scores["computed_by"] == "runtime-deterministic"

        # pages row upserted with the canonicalized URL
        with db.connect(autocommit=True) as c:
            page = c.execute("select * from pages where id=%s", (audit["page_id"],)).fetchone()
            costs = c.execute(
                "select * from cost_events where purpose='audit_classify' and org_id=%s",
                (org,),
            ).fetchall()
        assert page["url_norm"] == "https://example.com/page"
        assert audit["url"] == TARGET_URL  # pre-normalization URL kept
        assert len(costs) == 2  # one batched call per category (A, E)

    def test_transport_failure_is_honest_inconclusive(self, tmp_path, org_site):
        org, site = org_site
        reg = write_registry(tmp_path)
        llm = FakeLlm(respond_all_pass)

        audit_id = _run(org, site, llm, failing_fetcher_factory, reg)
        audit, findings = _rows(audit_id)

        assert audit["status"] == "inconclusive"
        assert audit["gate_state"] == "transport_inconclusive"
        assert findings == []  # no findings on an unreached page
        assert audit["scores"]["overall_grade"] == "INCONCLUSIVE"
        assert audit["scores"]["overall_score"] is None
        assert llm.calls == []  # no LLM spend on a transport failure

    def test_cost_cap_exhaustion_still_completes(self, tmp_path, org_site, public_dns):
        org, site = org_site
        reg = write_registry(tmp_path)
        llm = CapAfterLlm(FakeLlm(respond_all_pass), allow=1)

        audit_id = _run(org, site, llm, fake_fetcher_factory, reg, cap=1.0)
        audit, findings = _rows(audit_id)

        assert audit["status"] == "done"  # completes and grades what it has
        by_id = {f["check_id"]: f for f in findings}
        assert by_id["A-01"]["status"] == "pass"
        assert by_id["A-02"]["status"] == "pass"
        assert by_id["E-01"]["status"] == "inconclusive"
        assert by_id["E-01"]["evidence"]["note"] == "cost cap reached"
        assert by_id["E-01"]["evidence"]["source"] == "deterministic"
        # graded from what it has (category A only)
        assert audit["scores"]["section_scores"]["A_technical"] == 100.0
        assert audit["scores"]["overall_grade"] == "A+"
