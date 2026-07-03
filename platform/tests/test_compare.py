"""Tests for gm.audit.compare.

Pure rules (pick_competitors, compute_gaps, query_norm, handler validation)
run without a DB. The end-to-end tests run under the DATABASE_URL skip guard
with a FakeLlm, a fake fetcher factory, a fake serp snapshot function (the
real gm.intel.serp is built concurrently — compare imports it lazily and the
tests monkeypatch `compare._serp_get_snapshot`), and a mini registry. No
network; safety._getaddrinfo is patched for the BEV SSRF check.
"""

import json
import os
import re
import socket
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from psycopg.types.json import Jsonb

from gm import db
from gm.audit import compare, safety
from gm.audit.bev import NOT_FOUND_PATH
from gm.audit.fetch import FetchResult
from gm.audit.registry import Registry

# ---------------------------------------------------------------------------
# Mini registry (severity/weight chosen so gap ordering is observable)
# ---------------------------------------------------------------------------


def make_check(check_id: str, category: str, *, weight: float = 1,
               severity: str = "medium") -> dict:
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
        "severity": severity,
    }


CHECKS = [
    make_check("A-01", "A", weight=3, severity="high"),
    make_check("A-02", "A"),
    make_check("E-01", "E"),
]


def mini_registry() -> Registry:
    return Registry(version="vtest", checks={c["check_id"]: c for c in CHECKS})


# ---------------------------------------------------------------------------
# query_norm
# ---------------------------------------------------------------------------


def test_query_norm_collapses_case_and_whitespace():
    assert compare.query_norm("  Best   Med-Spa\tDubai ") == "best med-spa dubai"


# ---------------------------------------------------------------------------
# pick_competitors
# ---------------------------------------------------------------------------


def entry(rank, url, *, etype="organic", domain=None):
    e = {"rank": rank, "url": url, "title": f"r{rank}", "type": etype}
    if domain is not None:
        e["domain"] = domain
    return e


class TestPickCompetitors:
    def test_excludes_client_domain_and_subdomains(self):
        results = [
            entry(1, "https://comp-a.test/page"),
            entry(2, "https://blog.example.com/post"),        # client subdomain
            entry(3, "https://www.example.com/"),             # client (www)
            entry(4, "https://comp-b.test/page"),
        ]
        picked = compare.pick_competitors(results, "example.com")
        # The client's best rank is 2 (its own subdomain counts as the client);
        # only rank 1 is above it, and neither client entry is ever a candidate.
        assert [e["url"] for e in picked] == ["https://comp-a.test/page"]

    def test_client_subdomain_at_rank_one_means_no_competitors_above(self):
        results = [
            entry(1, "https://blog.example.com/post"),        # client already on top
            entry(2, "https://comp-a.test/page"),
        ]
        assert compare.pick_competitors(results, "example.com") == []

    def test_denylist_excluded_subdomain_aware(self):
        results = [
            entry(1, "https://www.instagram.com/somespa"),
            entry(2, "https://m.facebook.com/somespa"),
            entry(3, "https://comp-a.test/page"),
        ]
        picked = compare.pick_competitors(results, "example.com")
        assert [e["url"] for e in picked] == ["https://comp-a.test/page"]

    def test_non_auditable_types_excluded(self):
        results = [
            entry(1, "https://maps.host.test/x", etype="map"),
            entry(2, "https://video.host.test/x", etype="video"),
            entry(3, "https://comp-a.test/page"),
        ]
        picked = compare.pick_competitors(results, "example.com")
        assert [e["url"] for e in picked] == ["https://comp-a.test/page"]

    def test_absent_client_takes_top_entries_up_to_limit(self):
        results = [entry(i, f"https://comp-{i}.test/") for i in range(1, 6)]
        picked = compare.pick_competitors(results, "example.com", limit=3)
        assert [e["rank"] for e in picked] == [1, 2, 3]

    def test_only_entries_above_client(self):
        results = [
            entry(1, "https://comp-a.test/"),
            entry(2, "https://example.com/"),
            entry(3, "https://comp-b.test/"),
        ]
        picked = compare.pick_competitors(results, "example.com")
        assert [e["url"] for e in picked] == ["https://comp-a.test/"]

    def test_dedupes_hosts_and_skips_urlless_entries(self):
        results = [
            entry(1, "https://comp-a.test/one"),
            entry(2, "https://comp-a.test/two"),           # same host, deduped
            {"rank": 3, "title": "no url", "type": "organic"},
            entry(4, "https://comp-b.test/", domain="comp-b.test"),
        ]
        picked = compare.pick_competitors(results, "example.com")
        assert [e["url"] for e in picked] == ["https://comp-a.test/one", "https://comp-b.test/"]

    def test_client_match_uses_domain_field_when_present(self):
        results = [entry(1, "https://cdn.host.test/mirror", domain="example.com")]
        assert compare.pick_competitors(results, "example.com") == []


# ---------------------------------------------------------------------------
# compute_gaps
# ---------------------------------------------------------------------------


def findings(**statuses):
    return [{"check_id": cid, "status": st} for cid, st in statuses.items()]


class TestComputeGaps:
    def test_gap_requires_at_least_half_of_competitors_passing(self):
        client = findings(**{"A-01": "fail", "A-02": "fail"})
        comps = {
            "https://c1/": findings(**{"A-01": "pass", "A-02": "pass"}),
            "https://c2/": findings(**{"A-01": "pass", "A-02": "fail"}),
            "https://c3/": findings(**{"A-01": "fail", "A-02": "fail"}),
        }
        gaps = compare.compute_gaps(client, comps, mini_registry())
        # ceil(3/2)=2: A-01 passes on 2 comps -> gap; A-02 on 1 -> no gap.
        assert [g["check_id"] for g in gaps] == ["A-01"]
        assert gaps[0]["competitors_passing"] == 2
        assert gaps[0]["competitor_urls"] == ["https://c1/", "https://c2/"]
        assert gaps[0]["client_status"] == "fail"
        assert gaps[0]["name"] == "Check A-01"

    def test_client_warn_counts_but_competitor_warn_is_not_passing(self):
        client = findings(**{"E-01": "warn"})
        comps = {
            "https://c1/": findings(**{"E-01": "warn"}),
            "https://c2/": findings(**{"E-01": "pass"}),
        }
        gaps = compare.compute_gaps(client, comps, mini_registry())
        # ceil(2/2)=1: exactly one competitor passing is enough.
        assert [g["check_id"] for g in gaps] == ["E-01"]
        assert gaps[0]["competitors_passing"] == 1

    def test_client_pass_na_inconclusive_never_gap(self):
        client = findings(**{"A-01": "pass", "A-02": "na", "E-01": "inconclusive"})
        comps = {"https://c1/": findings(**{"A-01": "pass", "A-02": "pass", "E-01": "pass"})}
        assert compare.compute_gaps(client, comps, mini_registry()) == []

    def test_zero_audited_competitors_means_no_gaps(self):
        assert compare.compute_gaps(findings(**{"A-01": "fail"}), {}, mini_registry()) == []

    def test_ordered_by_severity_times_weight(self):
        # A-01: high(3) * weight 3 = 9; A-02/E-01: medium(2) * 1 = 2 (tie -> id order)
        client = findings(**{"E-01": "fail", "A-02": "fail", "A-01": "fail"})
        comps = {"https://c1/": findings(**{"A-01": "pass", "A-02": "pass", "E-01": "pass"})}
        gaps = compare.compute_gaps(client, comps, mini_registry())
        assert [g["check_id"] for g in gaps] == ["A-01", "A-02", "E-01"]

    def test_unknown_check_defaults_name_and_severity(self):
        client = [{"check_id": "Z-99", "status": "fail"}]
        comps = {"https://c1/": [{"check_id": "Z-99", "status": "pass"}]}
        gaps = compare.compute_gaps(client, comps, mini_registry())
        assert gaps[0]["name"] == "Z-99"
        assert gaps[0]["severity"] == "medium"


# ---------------------------------------------------------------------------
# handle_compare_serp (validation + wiring, no DB)
# ---------------------------------------------------------------------------


def make_ctx(payload, org_id="org", site_id="site"):
    return SimpleNamespace(
        job=SimpleNamespace(id=7, payload=payload, org_id=org_id, site_id=site_id),
        conn="fake-conn",
    )


class TestHandler:
    def test_missing_query_raises(self):
        with pytest.raises(ValueError, match="missing 'query'"):
            compare.handle_compare_serp(make_ctx({}))

    def test_missing_org_or_site_raises(self):
        with pytest.raises(ValueError, match="org_id and site_id"):
            compare.handle_compare_serp(make_ctx({"query": "q"}, org_id=None))

    def test_valid_payload_calls_run_comparison(self, monkeypatch):
        calls = {}

        def fake_run(conn, **kw):
            calls["conn"] = conn
            calls.update(kw)
            return "cmp-id"

        monkeypatch.setattr(compare, "run_comparison", fake_run)
        monkeypatch.setattr("gm.infra.llm.LlmClient", lambda: "fake-llm")
        compare.handle_compare_serp(make_ctx({"query": "best spa", "page": "https://x.test/p"}))
        assert calls["conn"] == "fake-conn"
        assert calls["query"] == "best spa"
        assert calls["client_page_url"] == "https://x.test/p"
        assert calls["org_id"] == "org" and calls["site_id"] == "site"
        assert calls["llm"] == "fake-llm"


# ---------------------------------------------------------------------------
# End-to-end (DB required)
# ---------------------------------------------------------------------------

db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

PAGE_HTML_TEMPLATE = """<html><head><title>{host}</title></head>
<body><h1>{host} services</h1>
<p>{host} provides med-spa treatments, consultations and aftercare guidance for
patients across the city. Our clinicians document procedures, publish pricing,
and answer the questions people actually ask before booking a consultation.</p>
</body></html>"""

NOT_FOUND_HTML = (
    "<html><head><title>404</title></head>"
    "<body><h1>Page not found</h1><p>This page does not exist.</p></body></html>"
)
ROBOTS_TXT = "User-agent: *\nAllow: /\n"
SITEMAP_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://example.com/</loc></url></urlset>"
)


def _result(url: str, status: int, text: str) -> FetchResult:
    return FetchResult(
        url=url, final_url=url, status_code=status,
        headers={"content-type": "text/html"},
        text=text, elapsed_ms=1, redirect_chain=[url],
    )


def fake_fetcher_factory(user_agent: str):
    def fetch(url: str) -> FetchResult:
        low = url.lower()
        host = urlsplit(low).hostname or ""
        if "comp-bad" in host:
            raise httpx.ConnectError("connection refused")
        if low.endswith("/robots.txt"):
            return _result(url, 200, ROBOTS_TXT)
        if low.endswith("/sitemap.xml"):
            return _result(url, 200, SITEMAP_XML)
        if NOT_FOUND_PATH.lower() in low:
            return _result(url, 404, NOT_FOUND_HTML)
        return _result(url, 200, PAGE_HTML_TEMPLATE.format(host=host))

    return fetch


@dataclass
class FakeResult:
    text: str
    parsed: object = None
    parse_error: str | None = None
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    cost_cents: float = 0.0
    model: str = "fake-model"


class FakeLlm:
    """Contract-shaped fake: classifies per the AUDITED PAGE url in the prompt —
    the client page fails A-01 and warns E-01; every other page passes all."""

    model = "fake-model"

    def __init__(self, client_host: str):
        self.client_host = client_host
        self.calls: list[str] = []

    def complete(self, *, system, user, max_tokens=4096, json_only=True, budget=None):
        self.calls.append(user)
        m = re.search(r"AUDITED PAGE: (\S+)", user)
        url = m.group(1) if m else ""
        is_client = self.client_host in url
        checks_block = user.split("EVIDENCE BUNDLE", 1)[0]
        ids = list(dict.fromkeys(re.findall(r'"check_id":"([A-J]-\d+)"', checks_block)))
        out = []
        for cid in ids:
            status = "pass"
            if is_client and cid == "A-01":
                status = "fail"
            elif is_client and cid == "E-01":
                status = "warn"
            out.append({"check_id": cid, "status": status, "note": "t"})
        text = json.dumps(out)
        return FakeResult(text=text, parsed=json.loads(text))


def fake_serp(results: list[dict]):
    """Stands in for gm.intel.serp.get_snapshot (built concurrently): inserts a
    real serp_snapshots row so the comparison's snapshot_id FK holds."""

    def _get(conn, site_id, query, *, client=None):
        row = conn.execute(
            "insert into serp_snapshots (org_id, site_id, query_norm, results, features)"
            " values (current_setting('app.org_id')::uuid, %s, %s, %s, '[]')"
            " returning id, fetched_at",
            (site_id, compare.query_norm(query), Jsonb(results)),
        ).fetchone()
        return {"id": str(row["id"]), "results": results, "features": [],
                "fetched_at": row["fetched_at"], "fresh": True}

    return _get


@pytest.fixture
def public_dns(monkeypatch):
    """bots_eye_view SSRF-validates targets — resolve every host publicly."""

    def resolve(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(safety, "_getaddrinfo", resolve)


@pytest.fixture(scope="session")
def _migrated():
    db.run_migrations()


@pytest.fixture
def org_site(_migrated):
    domain = f"client-{uuid.uuid4().hex[:10]}.test"
    with db.connect(autocommit=True) as c:
        org = c.execute("insert into orgs (name) values ('compare-test') returning id"
                        ).fetchone()["id"]
        site = c.execute(
            "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
            (org, domain),
        ).fetchone()["id"]
    return str(org), str(site), domain


def _run(org, site, domain, results, monkeypatch, query="best med spa"):
    monkeypatch.setattr(compare, "_serp_get_snapshot", fake_serp(results))
    llm = FakeLlm(domain)
    with db.connect() as conn:
        db.set_org(conn, org)
        cmp_id = compare.run_comparison(
            conn, org_id=org, site_id=site, query=query, llm=llm,
            registry=mini_registry(), fetcher_factory=fake_fetcher_factory,
        )
        conn.commit()
    return cmp_id


def _load(cmp_id):
    with db.connect(autocommit=True) as c:
        row = c.execute("select * from serp_comparisons where id=%s", (cmp_id,)).fetchone()
        audits = {}
        ids = [row["client_audit_id"], *row["competitor_audit_ids"]]
        for aid in ids:
            audits[str(aid)] = c.execute("select * from audits where id=%s", (aid,)).fetchone()
    return row, audits


@db_required
class TestEndToEnd:
    def test_full_comparison_flow(self, org_site, public_dns, monkeypatch):
        org, site, domain = org_site
        results = [
            entry(1, "https://comp-a.test/page"),
            entry(2, "https://comp-b.test/page"),
            entry(3, "https://www.instagram.com/spa"),        # denylisted
            entry(4, f"https://{domain}/page"),                # the client
            entry(5, "https://comp-c.test/page"),              # below the client
        ]
        cmp_id = _run(org, site, domain, results, monkeypatch)
        row, audits = _load(cmp_id)

        # Snapshot + client rank recorded.
        assert row["snapshot_id"] is not None
        assert row["query_norm"] == "best med spa"
        assert row["summary"]["client_rank"] == 4
        assert row["summary"]["client_url"] == f"https://{domain}/page"

        # Two competitors picked (above the client, denylist excluded).
        comp_ids = [str(a) for a in row["competitor_audit_ids"]]
        assert len(comp_ids) == 2
        comp_urls = {audits[a]["url"] for a in comp_ids}
        assert comp_urls == {"https://comp-a.test/page", "https://comp-b.test/page"}
        assert {r["url"] for r in row["summary"]["competitor_ranks"]} == comp_urls

        # Competitor audits: client's site_id, done, tagged competitor_reference
        # AFTER completion (original gate_state preserved in scores).
        for a in comp_ids:
            audit = audits[a]
            assert str(audit["site_id"]) == site
            assert audit["status"] == "done"
            assert audit["gate_state"] == compare.COMPETITOR_GATE_STATE
            assert audit["scores"]["original_gate_state"] == "ok"

        # Client audit is NOT tagged.
        client_audit = audits[str(row["client_audit_id"])]
        assert client_audit["gate_state"] == "ok"
        assert client_audit["status"] == "done"

        # Gap math: client fails A-01 / warns E-01, both competitors pass ->
        # gaps for both, high-impact first; A-02 passes everywhere -> no gap.
        gaps = row["gaps"]
        assert [g["check_id"] for g in gaps] == ["A-01", "E-01"]
        assert gaps[0]["client_status"] == "fail"
        assert gaps[0]["competitors_passing"] == 2
        assert sorted(gaps[0]["competitor_urls"]) == sorted(comp_urls)
        assert gaps[1]["client_status"] == "warn"

        assert row["summary"]["competitors_audited"] == 2
        assert row["summary"]["avg_scores"]["competitors"] == 100.0
        assert row["summary"]["client_audit_status"] == "done"

        # History hygiene: competitor rows are invisible to the latest-client-
        # audit lookup even for their own URL...
        with db.connect() as conn:
            db.set_org(conn, org)
            assert compare._latest_client_audit(conn, site, "https://comp-a.test/page") is None
            # ...and any client-history query filtering the tag excludes them.
            visible = conn.execute(
                "select id from audits where site_id=%s"
                " and coalesce(gate_state,'') <> %s",
                (site, compare.COMPETITOR_GATE_STATE),
            ).fetchall()
            conn.rollback()
        assert {str(r["id"]) for r in visible} == {str(row["client_audit_id"])}

        # Freshness reuse: a second comparison reuses the client audit.
        cmp2 = _run(org, site, domain, results, monkeypatch)
        row2, _ = _load(cmp2)
        assert row2["client_audit_id"] == row["client_audit_id"]

    def test_client_absent_and_broken_competitor(self, org_site, public_dns, monkeypatch):
        org, site, domain = org_site
        results = [
            entry(1, "https://comp-good.test/page"),
            entry(2, "https://comp-bad.test/page"),   # fetch raises -> inconclusive
        ]
        cmp_id = _run(org, site, domain, results, monkeypatch)
        row, audits = _load(cmp_id)

        # Client absent from the SERP: rank None, homepage audited.
        assert row["summary"]["client_rank"] is None
        assert row["summary"]["client_url"] == f"https://{domain}/"

        comp_by_url = {audits[str(a)]["url"]: audits[str(a)] for a in row["competitor_audit_ids"]}
        bad = comp_by_url["https://comp-bad.test/page"]
        good = comp_by_url["https://comp-good.test/page"]

        # The broken competitor is tagged too, but keeps its honest original state.
        assert bad["status"] == "inconclusive"
        assert bad["gate_state"] == compare.COMPETITOR_GATE_STATE
        assert bad["scores"]["original_gate_state"] == "transport_inconclusive"
        assert good["status"] == "done"

        # Gap denominator counts only AUDITED (done) competitors: n=1,
        # threshold ceil(1/2)=1 -> the one good competitor's pass is enough.
        assert row["summary"]["competitors_audited"] == 1
        gaps = row["gaps"]
        assert [g["check_id"] for g in gaps] == ["A-01", "E-01"]
        assert gaps[0]["competitors_passing"] == 1
        assert gaps[0]["competitor_urls"] == ["https://comp-good.test/page"]
