"""Phase D0 integration-surface tests (docs/phase-d0-contracts.md, agent C).

Covers:
  - pipeline: serp_context joins the evidence bundle as evidence["serp"],
    activates comparative (H) checks, persists a {"query", "client_rank"}
    summary into scores["serp_context"]; serp_context_for_page reads the
    freshest matching rank_history + serp_snapshots pair; handle_audit_page
    wires the context automatically.
  - report: masthead rank line ("ranks #7 for 'query'"), escaped, only when
    scores.serp_context is present; NULL rank renders honest absence.
  - receipts: rank_tracking payload section (lazy rank_tracker import,
    tolerated absence) + the 'Google visibility' section with rank arrows,
    AIO badges, competitor top-10 changes, and honest empty states.

Pure tests run everywhere; DB tests skip cleanly without DATABASE_URL.
ZERO network anywhere (FakeLlm + fake fetcher factory + patched DNS).
"""

import datetime as dt
import json
import os
import re
import socket
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from gm import db
from gm.audit import pipeline, safety
from gm.audit.registry import Registry, load_registry
from gm.delivery import receipts, report

db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


# ---------------------------------------------------------------------------
# Shared fixtures/helpers (test_draft_audit style)
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


SERP_CHECKS = [
    make_check("A-02", "A"),                                             # llm — gradeable
    make_check("H-01", "H", method="comparative", badge="comparative"),  # needs comparison
]


def serp_registry() -> Registry:
    return Registry(version="vserp", checks={c["check_id"]: c for c in SERP_CHECKS})


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
    checks_block = user.split("EVIDENCE BUNDLE", 1)[0]
    ids = list(dict.fromkeys(re.findall(r'"check_id":"([A-J]-\d+)"', checks_block)))
    return json.dumps([{"check_id": i, "status": "pass", "note": "ok"} for i in ids])


SERP_CONTEXT = {
    "query": "outpatient billing recovery",
    "results": [
        {"rank": 1, "url": "https://rival.example/guide", "domain": "rival.example",
         "title": "Rival guide", "type": "organic"},
        {"rank": 3, "url": "https://example.com/page", "domain": "example.com",
         "title": "Our page", "type": "organic"},
    ],
    "client_rank": 3,
    "features": [{"type": "people_also_ask", "questions": ["what is billing recovery"]}],
}


# ---------------------------------------------------------------------------
# Pure: serp evidence activates comparative checks
# ---------------------------------------------------------------------------


class TestComparativeSerpActivation:
    def test_serp_evidence_disables_comparative_overrides_real_registry(self):
        assert pipeline.comparative_na_overrides(load_registry(), {"serp": SERP_CONTEXT}) == {}

    def test_without_serp_h_checks_stay_na(self):
        overrides = pipeline.comparative_na_overrides(load_registry(), {})
        assert "H-01" in overrides
        assert overrides["H-01"]["note"] == pipeline.COMPARATIVE_NA_NOTE

    def test_empty_serp_context_counts_as_absent(self):
        overrides = pipeline.comparative_na_overrides(load_registry(), {"serp": {}})
        assert "H-01" in overrides

    def test_classify_h_checks_with_serp_context(self):
        llm = FakeLlm(respond_all_pass)
        reg = serp_registry()
        evidence = {"page": {"url": "https://example.com/page"}, "serp": SERP_CONTEXT}
        overrides = pipeline.comparative_na_overrides(reg, evidence)
        assert overrides == {}  # serp context = comparison data
        status_map, notes, cost = pipeline.classify_checks(
            llm, reg, evidence, pipeline.CallBudget(100.0), overrides=overrides
        )
        assert len(llm.calls) == 2  # categories A and H both prompted
        assert status_map["H-01"] == {"status": "pass", "note": "ok", "source": "llm"}
        prompts = "\n".join(user for _, user in llm.calls)
        assert "rival.example" in prompts  # the SERP reached the classifier
        assert "outpatient billing recovery" in prompts


# ---------------------------------------------------------------------------
# Pure: masthead rank line (report.py)
# ---------------------------------------------------------------------------


def _audit_row(scores: dict) -> dict:
    return {
        "url": "https://example.com/page",
        "gate_state": "ok",
        "registry_version": "r1",
        "model_version": "m1",
        "scores": {"overall_grade": "B+", "demand_capture": 81.3, **scores},
    }


class TestMastheadRankLine:
    SITE = {"domain_norm": "example.com"}

    def test_rank_line_rendered_when_serp_context_present(self):
        audit = _audit_row(
            {"serp_context": {"query": "best clinic dubai", "client_rank": 7}}
        )
        html_out = report.render_audit_html(audit, [], self.SITE)
        assert "ranks #7 for 'best clinic dubai'" in html_out

    def test_rank_line_query_is_escaped(self):
        audit = _audit_row(
            {"serp_context": {"query": "<script>alert(1)</script>", "client_rank": 2}}
        )
        html_out = report.render_audit_html(audit, [], self.SITE)
        assert "<script" not in html_out.lower()
        assert "ranks #2 for '&lt;script&gt;alert(1)&lt;/script&gt;'" in html_out

    def test_null_rank_renders_honest_absence_not_zero(self):
        audit = _audit_row(
            {"serp_context": {"query": "best clinic dubai", "client_rank": None}}
        )
        html_out = report.render_audit_html(audit, [], self.SITE)
        assert "ranks #" not in html_out
        assert "not in tracked Google depth for 'best clinic dubai'" in html_out

    def test_no_serp_context_no_rank_line(self):
        html_out = report.render_audit_html(_audit_row({}), [], self.SITE)
        assert "ranks #" not in html_out
        assert "tracked Google depth" not in html_out

    def test_forged_serp_context_shapes_tolerated(self):
        for forged in ({"serp_context": "evil"}, {"serp_context": {"client_rank": 5}},
                       {"serp_context": {"query": "", "client_rank": 5}}):
            html_out = report.render_audit_html(_audit_row(forged), [], self.SITE)
            assert "ranks #" not in html_out


# ---------------------------------------------------------------------------
# Pure: receipt 'Google visibility' section
# ---------------------------------------------------------------------------


def _receipt_payload(rank_tracking: dict) -> dict:
    return {
        "period": "2026-06",
        "prior_period": "2026-05",
        "audits": {"run": 0, "movement": {"first": None, "last": None, "change": None}},
        "rank_tracking": rank_tracking,
        "gsc": {"connected": False},
    }


MOVEMENT = [
    {"query": "best clinic dubai", "first_rank": 12, "last_rank": 7,
     "aio_cited_first": False, "aio_cited_last": True,
     "entered_top10": ["newrival.com"], "left_top10": ["oldrival.com"]},
    {"query": "clinic pricing", "first_rank": 4, "last_rank": 9,
     "aio_cited_first": False, "aio_cited_last": False,
     "entered_top10": [], "left_top10": []},
    {"query": "invisible query", "first_rank": None, "last_rank": None,
     "aio_cited_first": False, "aio_cited_last": False,
     "entered_top10": [], "left_top10": []},
]


class TestReceiptRankSection:
    SITE = {"domain_norm": "ex.com"}

    def test_section_rendered_before_beta_citations(self):
        payload = _receipt_payload({"available": True, "queries": MOVEMENT})
        payload["citations"] = {
            "prompts": [receipts.citation_entry("p1", "best clinic", (1, 9), (7, 9))],
            "controls": {"sites": [], "mean_abs_drift": None},
        }
        html_out = receipts.render_receipt_html(self.SITE, payload)
        assert "Google visibility" in html_out
        assert html_out.index("Google visibility") < html_out.index("AI citation rates")

    def test_rank_arrows_aio_badges_and_competitor_lines(self):
        html_out = receipts.render_receipt_html(
            self.SITE, _receipt_payload({"available": True, "queries": MOVEMENT})
        )
        # improved query: #12 -> #7, up arrow, gained AIO badge, competitor moves
        assert "#12 &rarr; #7" in html_out
        assert "delta-up" in html_out
        assert "AIO cited" in html_out and "(gained)" in html_out
        assert "entered top-10: newrival.com" in html_out
        assert "left top-10: oldrival.com" in html_out
        # declined query: #4 -> #9, down arrow
        assert "#4 &rarr; #9" in html_out
        assert "delta-down" in html_out
        # never-ranked query: honest absence, never a fabricated rank
        assert "not ranked" in html_out
        assert "#0 &rarr;" not in html_out
        assert "&rarr; #0" not in html_out

    def test_alias_keys_from_rank_tracker_are_tolerated(self):
        aliased = [{
            "query_norm": "aliased query", "rank_first": 8, "rank_last": 2,
            "aio_cited": {"first": True, "last": False},
            "competitors": {"entered": ["a.com"], "left": ["b.com"]},
        }]
        html_out = receipts.render_receipt_html(
            self.SITE, _receipt_payload({"available": True, "queries": aliased})
        )
        assert "aliased query" in html_out
        assert "#8 &rarr; #2" in html_out
        assert "lost AIO citation" in html_out
        assert "entered top-10: a.com" in html_out
        assert "left top-10: b.com" in html_out

    def test_honest_empty_state_no_tracked_queries(self):
        html_out = receipts.render_receipt_html(
            self.SITE, _receipt_payload({"available": True, "queries": []})
        )
        assert "Google visibility" in html_out
        assert "No tracked queries this period" in html_out

    def test_honest_state_when_module_unavailable(self):
        html_out = receipts.render_receipt_html(
            self.SITE, _receipt_payload({"available": False, "queries": []})
        )
        assert "Rank tracking is not enabled" in html_out

    def test_hostile_movement_values_escaped(self):
        hostile = "<script>alert(1)</script>"
        queries = [{"query": hostile, "first_rank": 1, "last_rank": 1,
                    "entered_top10": [hostile], "left_top10": []}]
        html_out = receipts.render_receipt_html(
            self.SITE, _receipt_payload({"available": True, "queries": queries})
        )
        assert "<script" not in html_out
        assert "&lt;script&gt;" in html_out


# ---------------------------------------------------------------------------
# Pure: handle_audit_page auto-wiring (no DB, no network)
# ---------------------------------------------------------------------------


class TestHandleAuditPageWiring:
    def _ctx(self, url: str) -> SimpleNamespace:
        return SimpleNamespace(
            conn=object(),
            job=SimpleNamespace(
                id=1, org_id=uuid.uuid4(), site_id=uuid.uuid4(), payload={"url": url}
            ),
        )

    def _patch(self, monkeypatch, context):
        helper_calls, run_calls = [], []

        def fake_helper(conn, site_id, url_norm):
            helper_calls.append((conn, site_id, url_norm))
            return context

        def fake_run(conn, **kwargs):
            run_calls.append(kwargs)
            return str(uuid.uuid4())

        class DummyLlm:
            model = "dummy"

        monkeypatch.setattr(pipeline, "serp_context_for_page", fake_helper)
        monkeypatch.setattr(pipeline, "run_page_audit", fake_run)
        monkeypatch.setattr("gm.infra.llm.LlmClient", DummyLlm)
        return helper_calls, run_calls

    def test_context_looked_up_with_canonical_url_and_passed_through(self, monkeypatch):
        helper_calls, run_calls = self._patch(monkeypatch, SERP_CONTEXT)
        ctx = self._ctx("HTTPS://Example.com/Page#frag")
        pipeline.handle_audit_page(ctx)
        (call,) = helper_calls
        assert call[0] is ctx.conn
        assert call[1] == ctx.job.site_id
        assert call[2] == "https://example.com/Page"  # canonicalized lookup key
        (kwargs,) = run_calls
        assert kwargs["serp_context"] is SERP_CONTEXT
        assert kwargs["url"] == "HTTPS://Example.com/Page#frag"  # original URL kept

    def test_absent_context_passes_none_behavior_unchanged(self, monkeypatch):
        _, run_calls = self._patch(monkeypatch, None)
        pipeline.handle_audit_page(self._ctx("https://example.com/page"))
        assert run_calls[0]["serp_context"] is None


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


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


def fake_fetcher_factory(user_agent: str):
    from gm.audit.bev import NOT_FOUND_PATH
    from gm.audit.fetch import FetchResult

    def _result(url: str, status: int, text: str) -> FetchResult:
        return FetchResult(
            url=url, final_url=url, status_code=status,
            headers={"content-type": "text/html"},
            text=text, elapsed_ms=1, redirect_chain=[url],
        )

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
        org = c.execute("insert into orgs (name) values ('d0-integration-test') returning id"
                        ).fetchone()["id"]
        site = c.execute(
            "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
            (org, f"example-{uuid.uuid4().hex[:10]}.com"),
        ).fetchone()["id"]
    return str(org), str(site)


def _snapshot(conn, org, site, query, results=None, features=None):
    from psycopg.types.json import Jsonb

    return conn.execute(
        "insert into serp_snapshots (org_id, site_id, query_norm, results, features)"
        " values (%s, %s, %s, %s, %s) returning id",
        (org, site, query, Jsonb(results or []), Jsonb(features or [])),
    ).fetchone()["id"]


def _rank_row(conn, org, site, query, checked_on, *, rank=None, ranked_url=None,
              snapshot_id=None):
    conn.execute(
        "insert into rank_history (org_id, site_id, query_norm, checked_on, rank,"
        " ranked_url, snapshot_id) values (%s, %s, %s, %s, %s, %s, %s)",
        (org, site, query, checked_on, rank, ranked_url, snapshot_id),
    )


def _tracked(conn, org, site, query, target_page=None):
    conn.execute(
        "insert into tracked_queries (org_id, site_id, query_norm, target_page)"
        " values (%s, %s, %s, %s)",
        (org, site, query, target_page),
    )


# ---------------------------------------------------------------------------
# DB: serp_context_for_page
# ---------------------------------------------------------------------------


@db_required
class TestSerpContextForPage:
    URL_NORM = "https://example.com/page"

    def test_freshest_matching_ranked_url_wins(self, org_site):
        org, site = org_site
        with db.connect(autocommit=True) as c:
            snap_old = _snapshot(c, org, site, "q1", results=[{"rank": 9}])
            snap_new = _snapshot(c, org, site, "q1", results=SERP_CONTEXT["results"],
                                 features=SERP_CONTEXT["features"])
            _rank_row(c, org, site, "q1", dt.date(2026, 6, 20), rank=9,
                      ranked_url="https://EXAMPLE.com/page#x", snapshot_id=snap_old)
            _rank_row(c, org, site, "q1", dt.date(2026, 6, 27), rank=3,
                      ranked_url="https://example.com/page", snapshot_id=snap_new)
            ctx = pipeline.serp_context_for_page(c, site, self.URL_NORM)
        assert ctx == {
            "query": "q1",
            "results": SERP_CONTEXT["results"],
            "client_rank": 3,
            "features": SERP_CONTEXT["features"],
        }

    def test_match_via_target_page_with_null_rank(self, org_site):
        org, site = org_site
        with db.connect(autocommit=True) as c:
            snap = _snapshot(c, org, site, "q2", results=[{"rank": 1, "domain": "r.com"}])
            _tracked(c, org, site, "q2", target_page="HTTPS://Example.com/page#frag")
            _rank_row(c, org, site, "q2", dt.date(2026, 6, 27), rank=None,
                      ranked_url=None, snapshot_id=snap)
            ctx = pipeline.serp_context_for_page(c, site, self.URL_NORM)
        assert ctx is not None
        assert ctx["query"] == "q2"
        assert ctx["client_rank"] is None  # honest absence, not 0

    def test_no_match_returns_none(self, org_site):
        org, site = org_site
        with db.connect(autocommit=True) as c:
            snap = _snapshot(c, org, site, "q3")
            _rank_row(c, org, site, "q3", dt.date(2026, 6, 27), rank=5,
                      ranked_url="https://example.com/other", snapshot_id=snap)
            assert pipeline.serp_context_for_page(c, site, self.URL_NORM) is None

    def test_rows_without_snapshot_are_skipped(self, org_site):
        org, site = org_site
        with db.connect(autocommit=True) as c:
            _rank_row(c, org, site, "q4", dt.date(2026, 6, 27), rank=2,
                      ranked_url=self.URL_NORM, snapshot_id=None)
            assert pipeline.serp_context_for_page(c, site, self.URL_NORM) is None


# ---------------------------------------------------------------------------
# DB: page audit end-to-end with serp_context (evidence + H checks + scores)
# ---------------------------------------------------------------------------


@db_required
class TestPageAuditWithSerpContext:
    def _run(self, org, site, llm, serp_context):
        with db.connect() as conn:
            db.set_org(conn, org)
            audit_id = pipeline.run_page_audit(
                conn, org_id=org, site_id=site, url=TARGET_URL, llm=llm,
                registry=serp_registry(), fetcher_factory=fake_fetcher_factory,
                serp_context=serp_context,
            )
            conn.commit()
        with db.connect(autocommit=True) as c:
            audit = c.execute("select * from audits where id=%s", (audit_id,)).fetchone()
            findings = c.execute(
                "select * from audit_findings where audit_id=%s order by check_id",
                (audit_id,),
            ).fetchall()
        return audit, findings

    def test_serp_context_classifies_h_and_round_trips_scores(self, org_site, public_dns):
        org, site = org_site
        llm = FakeLlm(respond_all_pass)
        audit, findings = self._run(org, site, llm, SERP_CONTEXT)

        assert audit["status"] == "done"
        assert audit["gate_state"] == "ok"
        by_id = {f["check_id"]: f for f in findings}
        # H-01 classified by the model (not deterministically 'na')
        assert by_id["H-01"]["status"] == "pass"
        assert by_id["H-01"]["evidence"]["source"] == "llm"
        assert by_id["A-02"]["status"] == "pass"

        # scores round-trip: {"query", "client_rank"} summary persisted
        assert audit["scores"]["serp_context"] == {
            "query": "outpatient billing recovery", "client_rank": 3,
        }

        # both categories prompted; the SERP reached the classifier as evidence
        assert len(llm.calls) == 2
        prompts = "\n".join(user for _, user in llm.calls)
        assert '"serp"' in prompts
        assert "rival.example" in prompts

        # and the persisted scores render the masthead rank line
        site_row = {"domain_norm": "example.com"}
        html_out = report.render_audit_html(dict(audit), findings, site_row)
        assert "ranks #3 for 'outpatient billing recovery'" in html_out

    def test_without_serp_context_h_stays_na_and_no_scores_key(self, org_site, public_dns):
        org, site = org_site
        llm = FakeLlm(respond_all_pass)
        audit, findings = self._run(org, site, llm, None)

        by_id = {f["check_id"]: f for f in findings}
        assert by_id["H-01"]["status"] == "na"
        assert by_id["H-01"]["evidence"]["note"] == pipeline.COMPARATIVE_NA_NOTE
        assert "serp_context" not in audit["scores"]
        assert len(llm.calls) == 1  # category H never reached the classifier


# ---------------------------------------------------------------------------
# DB: assemble_site_receipt rank_tracking section
# ---------------------------------------------------------------------------


@db_required
class TestAssembleReceiptRankTracking:
    PERIOD = "2026-06"

    def _payload(self, site):
        with db.connect(autocommit=True) as c:
            rid = receipts.assemble_site_receipt(c, site_id=site, period=self.PERIOD)
            return c.execute(
                "select payload from site_deltas where id = %s", (rid,)
            ).fetchone()["payload"]

    def test_module_absent_is_tolerated_honestly(self, org_site, monkeypatch):
        _, site = org_site
        monkeypatch.setattr(receipts, "_rank_movement_fn", lambda: None)
        payload = self._payload(site)
        assert payload["rank_tracking"]["available"] is False
        assert payload["rank_tracking"]["queries"] == []
        html_out = receipts.render_receipt_html({"domain_norm": "x.com"}, payload)
        assert "Rank tracking is not enabled" in html_out

    def test_movement_called_with_inclusive_period_bounds(self, org_site, monkeypatch):
        _, site = org_site
        calls = []

        def fake_movement(conn, site_id, *, since, until):
            calls.append((site_id, since, until))
            return MOVEMENT

        monkeypatch.setattr(receipts, "_rank_movement_fn", lambda: fake_movement)
        payload = self._payload(site)
        assert calls == [(site, dt.date(2026, 6, 1), dt.date(2026, 6, 30))]
        rt = payload["rank_tracking"]
        assert rt["available"] is True
        assert [q["query"] for q in rt["queries"]] == [
            "best clinic dubai", "clinic pricing", "invisible query",
        ]
        html_out = receipts.render_receipt_html({"domain_norm": "x.com"}, payload)
        assert "Google visibility" in html_out
        assert "#12 &rarr; #7" in html_out

    def test_no_tracked_queries_empty_state(self, org_site, monkeypatch):
        _, site = org_site
        monkeypatch.setattr(receipts, "_rank_movement_fn", lambda: lambda *a, **k: [])
        payload = self._payload(site)
        assert payload["rank_tracking"] == {"available": True, "queries": []}
        html_out = receipts.render_receipt_html({"domain_norm": "x.com"}, payload)
        assert "No tracked queries this period" in html_out
