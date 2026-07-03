"""Tests for the phase C wave-3 pipeline additions (docs/phase-c-wave3-contracts.md):

  - run_draft_audit: pre-publish scorecard — no fetch/BEV/robots/sitemap,
    crawl-dependent checks 'na' ("not applicable pre-publish"), schema
    inspector on the provided HTML, deterministic grading, draft_id round-trip.
  - comparative-N/A: method='comparative' checks become 'na' ("requires
    comparison data") in BOTH audit paths when evidence has no comparison
    section, and their categories cost no LLM calls.

The na-set tests run against the REAL registry (registry/checks/*.json).
End-to-end tests run under the DATABASE_URL skip guard with a FakeLlm and (for
the page path) a fake fetcher factory — no network, no live DNS.
"""

import json
import os
import re
import socket
import uuid
from dataclasses import dataclass, field

import pytest

from gm import db
from gm.audit import pipeline, safety
from gm.audit.bev import NOT_FOUND_PATH
from gm.audit.fetch import FetchResult
from gm.audit.registry import Registry, load_registry

# ---------------------------------------------------------------------------
# Mini registry with a method mix (llm / measured / comparative / explicit-id)
# ---------------------------------------------------------------------------


def make_check(check_id: str, category: str, *, method: str = "llm",
               badge: str = "static_rule") -> dict:
    return {
        "check_id": check_id,
        "check_version": 1,
        "category": category,
        "category_name": f"Category {category}",
        "name": f"Check {check_id}",
        "description": "sample check",
        "applies_to": ["all"],
        "method": method,
        "badge": badge,
        "fix_type": "page_html",
        "criteria": {"pass": "good", "warn": "meh", "fail": "bad"},
        "weight": 1,
        "severity": "medium",
    }


DRAFT_CHECKS = [
    make_check("A-02", "A"),                                             # llm — gradeable
    make_check("A-10", "A", method="deterministic", badge="hard_evidence"),  # explicit-id na
    make_check("B-01", "B", method="measured", badge="measured"),        # measured -> na
    make_check("C-06", "C", method="comparative", badge="comparative"),  # comparative -> na
    make_check("F-01", "F"),                                             # llm — gradeable
]


def draft_registry() -> Registry:
    return Registry(version="vdraft", checks={c["check_id"]: c for c in DRAFT_CHECKS})


PAGE_CHECKS = [
    make_check("A-02", "A"),
    make_check("C-06", "C", method="comparative", badge="comparative"),
]


def page_registry() -> Registry:
    return Registry(version="vpage", checks={c["check_id"]: c for c in PAGE_CHECKS})


# ---------------------------------------------------------------------------
# FakeLlm (contract shape; cost 0)
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
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system, user, max_tokens=4096, json_only=True, budget=None):
        self.calls.append((system, user))
        text = self._responses(system, user)
        parsed, err = None, None
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            err = str(exc)
        return FakeResult(text=text, parsed=parsed, parse_error=err)


def respond_all_pass(system: str, user: str) -> str:
    """Classify every check listed in the CHECKS block of the prompt as pass."""
    checks_block = user.split("EVIDENCE BUNDLE", 1)[0]
    ids = list(dict.fromkeys(re.findall(r'"check_id":"([A-J]-\d+)"', checks_block)))
    return json.dumps([{"check_id": i, "status": "pass", "note": "ok"} for i in ids])


# ---------------------------------------------------------------------------
# NA-set correctness against the REAL registry
# ---------------------------------------------------------------------------


class TestDraftNaSet:
    def test_measured_checks_are_in_the_set(self):
        na = pipeline.draft_na_check_ids(load_registry())
        measured = {"A-12", "B-01", "B-06", "B-10", "C-14", "E-05", "H-07", "I-02", "J-04"}
        assert measured <= na

    def test_explicit_crawl_dependent_ids_are_in_the_set(self):
        na = pipeline.draft_na_check_ids(load_registry())
        assert pipeline.DRAFT_NA_CHECK_IDS <= na
        # spot ids: robots/sitemap/site-file dependent
        for cid in ("A-10", "A-11", "E-01", "E-07", "E-08", "E-09", "E-13"):
            assert cid in na

    def test_content_judgeable_checks_stay_applicable(self):
        na = pipeline.draft_na_check_ids(load_registry())
        # tag/content-level checks must NOT be excluded pre-publish; C-06 is
        # comparative and handled by the comparative-N/A rule, not this set.
        for cid in ("A-02", "A-06", "C-06", "D-01", "E-04", "E-06", "E-11", "F-03", "G-02"):
            assert cid not in na

    def test_set_size_is_measured_plus_explicit(self):
        reg = load_registry()
        measured = {cid for cid, c in reg.checks.items() if c.get("method") == "measured"}
        assert pipeline.draft_na_check_ids(reg) == frozenset(
            measured | pipeline.DRAFT_NA_CHECK_IDS
        )

    def test_restricted_to_registry(self):
        reg = draft_registry()  # only knows A-02/A-10/B-01/C-06/F-01
        assert pipeline.draft_na_check_ids(reg) == frozenset({"A-10", "B-01"})


class TestComparativeOverrides:
    def test_real_registry_comparative_ids(self):
        overrides = pipeline.comparative_na_overrides(load_registry(), {})
        assert set(overrides) == {"C-06", "H-01", "H-03", "H-04", "H-05", "H-06"}
        for entry in overrides.values():
            assert entry == {
                "status": "na", "note": "requires comparison data", "source": "deterministic",
            }

    def test_no_overrides_when_comparison_section_present(self):
        evidence = {"comparison": {"competitors": [{"url": "https://rival.example"}]}}
        assert pipeline.comparative_na_overrides(load_registry(), evidence) == {}

    def test_empty_comparison_section_counts_as_absent(self):
        overrides = pipeline.comparative_na_overrides(load_registry(), {"comparison": {}})
        assert "C-06" in overrides


class TestClassifyWithOverrides:
    def test_overridden_checks_skip_prompts_and_calls(self):
        llm = FakeLlm(respond_all_pass)
        reg = draft_registry()
        overrides = pipeline.comparative_na_overrides(reg, {})
        overrides.update({
            cid: {"status": "na", "note": pipeline.DRAFT_NA_NOTE, "source": "deterministic"}
            for cid in pipeline.draft_na_check_ids(reg)
        })
        status_map, notes, cost = pipeline.classify_checks(
            llm, reg, {}, pipeline.CallBudget(100.0), overrides=overrides
        )
        # only categories with un-overridden checks (A: A-02, F: F-01) were called
        assert len(llm.calls) == 2
        prompts = "\n".join(user for _, user in llm.calls)
        for cid in ("A-10", "B-01", "C-06"):
            assert cid not in prompts
        assert status_map["A-02"]["status"] == "pass"
        assert status_map["F-01"]["status"] == "pass"
        assert status_map["A-10"]["note"] == pipeline.DRAFT_NA_NOTE
        assert status_map["B-01"]["note"] == pipeline.DRAFT_NA_NOTE
        assert status_map["C-06"]["note"] == pipeline.COMPARATIVE_NA_NOTE
        assert cost == 0.0


# ---------------------------------------------------------------------------
# End-to-end (DB required)
# ---------------------------------------------------------------------------

db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

DRAFT_HTML = """<html><head><title>Outpatient billing recovery, explained</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization","name":"Acme","url":"https://example.com"}
</script></head><body><h1>Outpatient billing recovery</h1>
<p>Acme recovers stale receivables for ambulatory clinics: entitlement checks,
dunning tracks, and payer follow-up, so delivered care actually gets paid.</p>
</body></html>"""

URL_HINT = "https://example.com/blog/outpatient-billing-recovery"

PAGE_HTML = """<html><head><title>Acme Care</title></head>
<body><h1>Acme outpatient services</h1>
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
TARGET_URL = "https://example.com/page"


def _result(url: str, status: int, text: str) -> FetchResult:
    return FetchResult(
        url=url, final_url=url, status_code=status,
        headers={"content-type": "text/html"},
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
        org = c.execute("insert into orgs (name) values ('draft-audit-test') returning id"
                        ).fetchone()["id"]
        site = c.execute(
            "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
            (org, f"example-{uuid.uuid4().hex[:10]}.com"),
        ).fetchone()["id"]
    return str(org), str(site)


def _rows(audit_id):
    with db.connect(autocommit=True) as c:
        audit = c.execute("select * from audits where id=%s", (audit_id,)).fetchone()
        findings = c.execute(
            "select * from audit_findings where audit_id=%s order by check_id", (audit_id,)
        ).fetchall()
    return audit, findings


@db_required
class TestDraftAuditEndToEnd:
    def test_draft_grading_with_na_sets(self, org_site):
        org, site = org_site
        llm = FakeLlm(respond_all_pass)
        draft_id = str(uuid.uuid4())

        with db.connect() as conn:
            db.set_org(conn, org)
            audit_id = pipeline.run_draft_audit(
                conn, org_id=org, site_id=site, draft_html=DRAFT_HTML,
                url_hint=URL_HINT, llm=llm, registry=draft_registry(),
                draft_id=draft_id,
            )
            conn.commit()
        audit, findings = _rows(audit_id)

        # audits row: draft identity, no page, gate_state 'draft'
        assert audit["status"] == "done"
        assert audit["gate_state"] == "draft"
        assert str(audit["draft_id"]) == draft_id  # round-trip
        assert audit["page_id"] is None
        assert audit["url"] == URL_HINT
        assert audit["registry_version"] == "vdraft"
        assert audit["model_version"] == "fake-model"
        assert audit["finished_at"] is not None

        by_id = {f["check_id"]: f for f in findings}
        assert sorted(by_id) == ["A-02", "A-10", "B-01", "C-06", "F-01"]
        # classified from the draft evidence
        assert by_id["A-02"]["status"] == "pass"
        assert by_id["F-01"]["status"] == "pass"
        assert by_id["A-02"]["evidence"]["source"] == "llm"
        # not applicable pre-publish (explicit id + measured)
        for cid in ("A-10", "B-01"):
            assert by_id[cid]["status"] == "na"
            assert by_id[cid]["evidence"]["note"] == "not applicable pre-publish"
            assert by_id[cid]["evidence"]["source"] == "deterministic"
        # comparative without comparison data
        assert by_id["C-06"]["status"] == "na"
        assert by_id["C-06"]["evidence"]["note"] == "requires comparison data"
        assert by_id["C-06"]["evidence"]["source"] == "deterministic"

        # deterministic grading over the applicable pool only
        scores = audit["scores"]
        assert scores["gate_state"] == "draft"
        assert scores["overall_grade"] == "A+"
        assert scores["page_citation_readiness"] == 100.0
        assert scores["section_scores"]["A_technical"] == 100.0
        assert scores["section_scores"]["B_performance"] is None  # nothing applicable
        assert scores["computed_by"] == "runtime-deterministic"

        # only categories A and F were prompted; the schema inspector ran on
        # the provided HTML and its result reached the classifier evidence
        assert len(llm.calls) == 2
        prompts = "\n".join(user for _, user in llm.calls)
        assert '"schema"' in prompts and "Organization" in prompts
        assert URL_HINT in prompts
        assert "robots" not in prompts.lower()  # no crawl evidence on a draft

    def test_draft_failure_reaches_terminal_status(self, org_site):
        org, site = org_site

        class BoomLlm:
            model = "boom"

            def complete(self, **kwargs):
                raise RuntimeError("boom")

        # classify_checks shields per-category errors; force a stage-level
        # crash instead via a registry that breaks checks_by_category.
        class BrokenRegistry:
            version = "vbroken"
            checks = None  # .items() will raise inside the stage body

        with db.connect() as conn:
            db.set_org(conn, org)
            audit_id = pipeline.run_draft_audit(
                conn, org_id=org, site_id=site, draft_html="<html></html>",
                url_hint=URL_HINT, llm=BoomLlm(), registry=BrokenRegistry(),
            )
            conn.commit()
        audit, findings = _rows(audit_id)
        assert audit["status"] == "failed"
        assert audit["gate_state"] == "pipeline_error"
        assert findings == []


@db_required
class TestComparativeNaInPageAuditPath:
    def test_page_audit_marks_comparative_na(self, org_site, public_dns):
        org, site = org_site
        llm = FakeLlm(respond_all_pass)

        with db.connect() as conn:
            db.set_org(conn, org)
            audit_id = pipeline.run_page_audit(
                conn, org_id=org, site_id=site, url=TARGET_URL, llm=llm,
                registry=page_registry(), fetcher_factory=fake_fetcher_factory,
            )
            conn.commit()
        audit, findings = _rows(audit_id)

        assert audit["status"] == "done"
        assert audit["gate_state"] == "ok"
        by_id = {f["check_id"]: f for f in findings}
        assert by_id["A-02"]["status"] == "pass"
        assert by_id["C-06"]["status"] == "na"
        assert by_id["C-06"]["evidence"]["note"] == "requires comparison data"
        assert by_id["C-06"]["evidence"]["source"] == "deterministic"
        # category C never reached the classifier
        assert len(llm.calls) == 1
        assert "C-06" not in llm.calls[0][1]
