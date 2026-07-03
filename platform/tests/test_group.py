"""Tests for gm.audit.group — the group autopsy rollup.

Assembly rules are pure and pinned against findings fixtures without a DB:
sitewide threshold math (incl. the ceil boundaries at 3-of-5 and 2-of-3),
fix_type gating, dedupe, fix_queue ordering, and inconclusive pages excluded
from the denominator. The end-to-end tests run under the DATABASE_URL skip
guard with a FakeLlm + fake fetchers for 3 URLs (2 sharing a planted sitewide
failure) — no network, no live DNS (safety._getaddrinfo is patched).
"""

import json
import os
import re
import socket
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest

from gm import db
from gm.audit import group, safety
from gm.audit.bev import NOT_FOUND_PATH
from gm.audit.fetch import FetchResult
from gm.audit.registry import Registry, load_registry

# ---------------------------------------------------------------------------
# Mini registry (varied fix types / severities / weights)
# ---------------------------------------------------------------------------


def make_check(check_id: str, category: str, *, fix_type: str = "page_html",
               severity: str = "medium", weight: float = 1) -> dict:
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
        "fix_type": fix_type,
        "criteria": {"pass": "good", "warn": "meh", "fail": "bad"},
        "weight": weight,
        "severity": severity,
    }


GROUP_CHECKS = [
    make_check("A-01", "A", severity="high", weight=2),                       # impact 6
    make_check("A-02", "A", severity="low"),                                  # impact 1
    make_check("C-01", "C", fix_type="sitewide_template", severity="critical",
               weight=2),                                                     # impact 8
    make_check("D-01", "D", fix_type="schema", severity="high"),              # impact 3
    make_check("E-01", "E", fix_type="cms_constraint", severity="medium"),    # impact 2
]


def mini_registry() -> Registry:
    return Registry(version="vtest", checks={c["check_id"]: c for c in GROUP_CHECKS})


# ---------------------------------------------------------------------------
# Row fixtures (the shape load_group_rows produces)
# ---------------------------------------------------------------------------


def finding(check_id: str, status: str = "fail", note: str = "") -> dict:
    check = next(c for c in GROUP_CHECKS if c["check_id"] == check_id)
    return {
        "check_id": check_id,
        "status": status,
        "badge": check["badge"],
        "fix_type": check["fix_type"],
        "evidence": {"note": note, "source": "llm"},
    }


def row(url: str, *, score: float | None = 90.0, grade: str = "A",
        status: str = "done", findings: list | None = None) -> dict:
    scores = (
        {"overall_score": score, "overall_grade": grade}
        if score is not None
        else {"overall_score": None, "overall_grade": "INCONCLUSIVE", "inconclusive": True}
    )
    return {
        "audit_id": f"aid-{url.rsplit('/', 1)[-1]}",
        "url": url,
        "status": status,
        "gate_state": "ok" if status == "done" else "transport_inconclusive",
        "scores": scores,
        "findings": findings or [],
    }


def urls(n: int) -> list[str]:
    return [f"https://example.com/loc{i + 1}" for i in range(n)]


# ---------------------------------------------------------------------------
# Threshold math
# ---------------------------------------------------------------------------


class TestThreshold:
    def test_exact_integer_ceil(self):
        # ceil(0.6 * n) without float rounding traps
        assert group.sitewide_threshold(1) == 1
        assert group.sitewide_threshold(2) == 2   # ceil(1.2)
        assert group.sitewide_threshold(3) == 2   # ceil(1.8) — contract boundary
        assert group.sitewide_threshold(4) == 3   # ceil(2.4)
        assert group.sitewide_threshold(5) == 3   # ceil(3.0) — contract boundary
        assert group.sitewide_threshold(10) == 6

    def test_boundary_3_of_5_pages(self):
        u = urls(5)
        rows = [row(x, findings=[finding("C-01", note="broken template")]) for x in u[:3]]
        rows += [row(x) for x in u[3:]]
        out = group.assemble_rows(rows, mini_registry())
        assert [e["check_id"] for e in out["sitewide"]] == ["C-01"]
        assert out["sitewide"][0]["pages_affected"] == 3
        assert out["sitewide"][0]["affected_urls"] == u[:3]

    def test_below_boundary_2_of_5_is_per_location(self):
        u = urls(5)
        rows = [row(x, findings=[finding("C-01")]) for x in u[:2]]
        rows += [row(x) for x in u[2:]]
        out = group.assemble_rows(rows, mini_registry())
        assert out["sitewide"] == []
        assert [p["url"] for p in out["per_location_issues"]] == u[:2]
        assert all(p["check_ids"] == ["C-01"] for p in out["per_location_issues"])
        queue = out["fix_queue"]
        assert len(queue) == 1
        assert queue[0]["scope"] == "per_location"
        assert queue[0]["pages_affected"] == 2

    def test_boundary_2_of_3_pages(self):
        u = urls(3)
        rows = [row(x, findings=[finding("C-01")]) for x in u[:2]] + [row(u[2])]
        out = group.assemble_rows(rows, mini_registry())
        assert [e["check_id"] for e in out["sitewide"]] == ["C-01"]
        assert out["sitewide"][0]["pages_affected"] == 2

    def test_1_of_3_is_per_location(self):
        u = urls(3)
        rows = [row(u[0], findings=[finding("C-01")]), row(u[1]), row(u[2])]
        out = group.assemble_rows(rows, mini_registry())
        assert out["sitewide"] == []
        assert out["per_location_issues"] == [
            {"audit_id": "aid-loc1", "url": u[0], "check_ids": ["C-01"]}
        ]


class TestSitewideRules:
    def test_dedupe_one_entry_with_representative_note(self):
        u = urls(3)
        notes = ["", "missing LocalBusiness template block", "another note"]
        rows = [row(x, findings=[finding("C-01", note=n)]) for x, n in zip(u, notes, strict=True)]
        out = group.assemble_rows(rows, mini_registry())

        assert len(out["sitewide"]) == 1
        entry = out["sitewide"][0]
        assert entry["pages_affected"] == 3
        assert entry["affected_urls"] == u
        # first NON-EMPTY note wins as the representative
        assert entry["evidence_note"] == "missing LocalBusiness template block"
        # ONE fix_queue entry, not three
        c01 = [e for e in out["fix_queue"] if e["check_id"] == "C-01"]
        assert len(c01) == 1
        assert c01[0]["scope"] == "sitewide"
        assert c01[0]["pages_affected"] == 3
        assert "one fix, 3 pages benefit" in c01[0]["effort_hint"]

    def test_wrong_fix_type_never_sitewide_even_failing_everywhere(self):
        u = urls(3)
        rows = [row(x, findings=[finding("A-01")]) for x in u]  # page_html on 3/3
        out = group.assemble_rows(rows, mini_registry())
        assert out["sitewide"] == []
        # ...but it is NOT dropped: per-location on every page, one queue entry
        assert len(out["per_location_issues"]) == 3
        assert [e["check_id"] for e in out["fix_queue"]] == ["A-01"]
        assert out["fix_queue"][0]["scope"] == "per_location"
        assert out["fix_queue"][0]["pages_affected"] == 3

    def test_inconclusive_pages_excluded_from_denominator(self):
        u = urls(5)
        graded = [row(x, findings=[finding("C-01")]) for x in u[:2]] + [row(u[2])]
        dead = [row(x, score=None, status="inconclusive") for x in u[3:]]
        out = group.assemble_rows(graded + dead, mini_registry())

        # 3 graded pages -> threshold 2 -> C-01 failing on 2 IS sitewide;
        # with all 5 in the denominator (threshold 3) it would not have been.
        assert [e["check_id"] for e in out["sitewide"]] == ["C-01"]
        assert out["rollup"]["pages_audited"] == 5
        assert out["rollup"]["pages_graded"] == 3
        assert out["rollup"]["pages_inconclusive"] == 2
        assert out["rollup"]["grade_distribution"]["INCONCLUSIVE"] == 2

    def test_empty_and_all_inconclusive_groups(self):
        assert group.assemble_rows([], mini_registry())["rollup"]["pages_audited"] == 0
        rows = [row(x, score=None, status="inconclusive") for x in urls(2)]
        out = group.assemble_rows(rows, mini_registry())
        assert out["sitewide"] == []
        assert out["fix_queue"] == []
        assert out["rollup"]["avg_score"] is None
        assert out["rollup"]["pages_inconclusive"] == 2


class TestRollupAndLocations:
    def test_rollup_math_and_grade_distribution(self):
        u = urls(3)
        rows = [
            row(u[0], score=90.0, grade="A"),
            row(u[1], score=80.0, grade="B+"),
            row(u[2], score=70.0, grade="C+"),
        ]
        out = group.assemble_rows(rows, mini_registry())
        r = out["rollup"]
        assert r["avg_score"] == 80.0
        assert r["min_score"] == 70.0
        assert r["max_score"] == 90.0
        assert r["grade_distribution"] == {"A": 1, "B+": 1, "C+": 1}
        assert out["member_audit_ids"] == ["aid-loc1", "aid-loc2", "aid-loc3"]

    def test_top_issues_worst_three_by_severity_times_weight(self):
        # impacts: C-01=8, A-01=6, D-01=3, A-02=1 -> A-02 misses the cut
        fails = [finding(c) for c in ("A-02", "D-01", "C-01", "A-01")]
        out = group.assemble_rows([row(urls(1)[0], findings=fails)], mini_registry())
        top = out["locations"][0]["top_issues"]
        assert [t["check_id"] for t in top] == ["C-01", "A-01", "D-01"]
        assert top[0]["severity"] == "critical"
        assert top[0]["name"] == "Check C-01"

    def test_failed_location_kept_with_status(self):
        u = urls(2)
        rows = [row(u[0]), row(u[1], score=None, status="failed")]
        out = group.assemble_rows(rows, mini_registry())
        assert [loc["status"] for loc in out["locations"]] == ["done", "failed"]
        assert out["locations"][1]["grade"] == "INCONCLUSIVE"
        assert out["locations"][1]["score"] is None


class TestFixQueueOrdering:
    def test_sitewide_first_then_per_location_by_severity(self):
        u = urls(3)
        rows = [
            # C-01 sitewide on 3/3; D-01 (schema) sitewide on 2/3 (threshold 2)
            row(u[0], findings=[finding("C-01"), finding("D-01"), finding("A-02")]),
            row(u[1], findings=[finding("C-01"), finding("D-01"), finding("A-01")]),
            row(u[2], findings=[finding("C-01")]),
        ]
        out = group.assemble_rows(rows, mini_registry())

        queue = out["fix_queue"]
        assert [e["check_id"] for e in queue] == ["C-01", "D-01", "A-01", "A-02"]
        assert [e["scope"] for e in queue] == [
            "sitewide", "sitewide", "per_location", "per_location",
        ]
        # sitewide ordered by pages_affected; per-location by severity impact
        assert queue[0]["pages_affected"] == 3
        assert queue[1]["pages_affected"] == 2
        assert queue[2]["severity"] == "high"
        assert queue[3]["severity"] == "low"
        for e in queue:
            assert set(e) >= {
                "check_id", "name", "fix_type", "badge", "pages_affected", "urls", "effort_hint",
            }
            assert len(e["urls"]) == e["pages_affected"]


# ---------------------------------------------------------------------------
# run_group_audit sequencing (pipeline reuse, no DB)
# ---------------------------------------------------------------------------


class TestRunGroupAudit:
    def test_runs_pipeline_per_url_in_order_and_assembles(self, monkeypatch):
        seen: list[tuple[str, float]] = []

        def fake_run_page_audit(conn, **kw):
            seen.append((kw["url"], kw["cost_cap_cents"]))
            return f"id-{len(seen)}"

        def fake_assemble(conn, audit_ids, registry=None):
            return {"member_audit_ids": list(audit_ids), "registry_version": registry.version}

        monkeypatch.setattr(group, "run_page_audit", fake_run_page_audit)
        monkeypatch.setattr(group, "assemble_group", fake_assemble)

        out = group.run_group_audit(
            None, org_id="o", site_id="s", urls=["u1", "u2", "u3"], llm=object(),
            registry=mini_registry(), cost_cap_cents_per_page=42.0,
        )
        assert seen == [("u1", 42.0), ("u2", 42.0), ("u3", 42.0)]
        assert out["member_audit_ids"] == ["id-1", "id-2", "id-3"]
        assert out["registry_version"] == "vtest"

    def test_empty_urls_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            group.run_group_audit(
                None, org_id="o", site_id="s", urls=[], llm=object(), registry=mini_registry(),
            )


class TestHandlerValidation:
    def _ctx(self, payload, org_id="org", site_id="site"):
        job = SimpleNamespace(id=7, payload=payload, org_id=org_id, site_id=site_id)
        return SimpleNamespace(job=job, conn=None)

    def test_missing_urls_rejected(self):
        for bad in ({}, {"urls": []}, {"urls": "https://x.com"}, {"urls": [1, 2]}):
            with pytest.raises(ValueError, match="urls"):
                group.handle_audit_group(self._ctx(bad))

    def test_org_and_site_required(self):
        with pytest.raises(ValueError, match="org_id and site_id"):
            group.handle_audit_group(self._ctx({"urls": ["https://x.com"]}, org_id=None))
        with pytest.raises(ValueError, match="org_id and site_id"):
            group.handle_audit_group(self._ctx({"urls": ["https://x.com"]}, site_id=None))


# ---------------------------------------------------------------------------
# End-to-end (DB required)
# ---------------------------------------------------------------------------

db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

PAGE_HTML = """<html><head><title>Acme Clinic</title></head>
<body><h1>Acme outpatient clinic</h1>
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
    "<url><loc>https://example.com/loc1</loc></url></urlset>"
)
GROUP_URLS = [
    "https://example.com/loc1",
    "https://example.com/loc2",
    "https://example.com/loc3",
]


def write_registry(tmp_path) -> Registry:
    """A two-check registry through the real loader: one page-level check (A)
    and one sitewide-template check (C) to plant the shared failure on."""
    (tmp_path / "checks").mkdir()
    (tmp_path / "manifest.json").write_text(json.dumps({"version": "vtest"}))
    a = [make_check("A-01", "A", severity="high", weight=2)]
    c = [make_check("C-01", "C", fix_type="sitewide_template", severity="critical", weight=2)]
    (tmp_path / "checks" / "a.json").write_text(json.dumps(a))
    (tmp_path / "checks" / "c.json").write_text(json.dumps(c))
    return load_registry(tmp_path)


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

    def __init__(self, responder):
        self._responder = responder
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system, user, max_tokens=4096, json_only=True, budget=None):
        self.calls.append((system, user))
        text = self._responder(system, user)
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        return FakeResult(text=text, parsed=parsed)


def planted_sitewide_responder(system: str, user: str) -> str:
    """Fail C-01 on loc1 + loc2 (the planted sitewide failure); pass the rest."""
    checks_block = user.split("EVIDENCE BUNDLE", 1)[0]
    ids = list(dict.fromkeys(re.findall(r'"check_id":"([A-J]-\d+)"', checks_block)))
    on_planted_page = "/loc1" in user or "/loc2" in user
    out = []
    for cid in ids:
        if cid == "C-01" and on_planted_page:
            out.append({"check_id": cid, "status": "fail", "note": "template block missing"})
        else:
            out.append({"check_id": cid, "status": "pass", "note": "ok"})
    return json.dumps(out)


def _result(url: str, status: int, text: str) -> FetchResult:
    return FetchResult(
        url=url, final_url=url, status_code=status,
        headers={"content-type": "text/html"},
        text=text, elapsed_ms=1, redirect_chain=[url],
    )


def fake_fetcher_factory(user_agent: str):
    def fetch(url: str) -> FetchResult:
        low = url.lower()
        if "/down" in low:
            raise httpx.ConnectError("connection refused")
        if low.endswith("/robots.txt"):
            return _result(url, 200, ROBOTS_TXT)
        if low.endswith("/sitemap.xml"):
            return _result(url, 200, SITEMAP_XML)
        if NOT_FOUND_PATH.lower() in low:
            return _result(url, 404, NOT_FOUND_HTML)
        if re.search(r"/loc\d$", low):
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
        org = c.execute(
            "insert into orgs (name) values ('group-test') returning id"
        ).fetchone()["id"]
        site = c.execute(
            "insert into sites (org_id, domain_norm) values (%s, %s) returning id",
            (org, f"example-{uuid.uuid4().hex[:10]}.com"),
        ).fetchone()["id"]
    return str(org), str(site)


def _run_group(org, site, reg, page_urls):
    llm = FakeLlm(planted_sitewide_responder)
    with db.connect() as conn:
        db.set_org(conn, org)
        assembled = group.run_group_audit(
            conn, org_id=org, site_id=site, urls=page_urls, llm=llm,
            registry=reg, fetcher_factory=fake_fetcher_factory,
            cost_cap_cents_per_page=100.0,
        )
        group_id = group.persist_group_summary(
            conn, org_id=org, site_id=site, assembled=assembled,
            model_version=llm.model,
        )
        conn.commit()
    return assembled, group_id


@db_required
class TestEndToEnd:
    def test_group_of_three_with_planted_sitewide_failure(
        self, tmp_path, org_site, public_dns
    ):
        org, site = org_site
        reg = write_registry(tmp_path)
        assembled, group_id = _run_group(org, site, reg, GROUP_URLS)

        # every location present, graded, in order
        assert [loc["url"] for loc in assembled["locations"]] == GROUP_URLS
        assert all(loc["status"] == "done" for loc in assembled["locations"])
        assert assembled["rollup"]["pages_audited"] == 3
        assert assembled["rollup"]["pages_inconclusive"] == 0

        # the planted failure: 2 of 3 graded pages >= ceil(60%)=2 -> ONE
        # sitewide entry with pages_affected=2, not two per-location entries
        assert [e["check_id"] for e in assembled["sitewide"]] == ["C-01"]
        entry = assembled["sitewide"][0]
        assert entry["pages_affected"] == 2
        assert entry["affected_urls"] == GROUP_URLS[:2]
        assert entry["evidence_note"] == "template block missing"
        assert assembled["per_location_issues"] == []
        assert assembled["fix_queue"][0]["check_id"] == "C-01"
        assert assembled["fix_queue"][0]["scope"] == "sitewide"

        # loc1/loc2 graded down by the fail; loc3 clean
        assert assembled["locations"][0]["top_issues"][0]["check_id"] == "C-01"
        assert assembled["locations"][2]["top_issues"] == []
        assert assembled["locations"][2]["grade"] == "A+"

        # the ONE group summary audits row
        with db.connect(autocommit=True) as c:
            grow = c.execute("select * from audits where id=%s", (group_id,)).fetchone()
        assert grow["url"] is None
        assert grow["gate_state"] == "group_rollup"
        assert grow["status"] == "done"
        assert grow["registry_version"] == "vtest"
        assert grow["model_version"] == "fake-model"
        scores = grow["scores"]
        assert scores["member_audit_ids"] == assembled["member_audit_ids"]
        assert len(scores["member_audit_ids"]) == 3
        assert scores["rollup"]["pages_audited"] == 3
        assert [e["check_id"] for e in scores["sitewide"]] == ["C-01"]

        # member audits really exist and are the ones the rollup points at
        with db.connect(autocommit=True) as c:
            members = c.execute(
                "select id, status from audits where id = any(%s::uuid[])",
                (scores["member_audit_ids"],),
            ).fetchall()
        assert len(members) == 3
        assert all(m["status"] == "done" for m in members)

    def test_unreachable_location_included_and_out_of_denominator(
        self, tmp_path, org_site, public_dns
    ):
        org, site = org_site
        reg = write_registry(tmp_path)
        page_urls = GROUP_URLS + ["https://example.com/down"]
        assembled, group_id = _run_group(org, site, reg, page_urls)

        # the dead page is included with its honest status, never dropped
        assert assembled["rollup"]["pages_audited"] == 4
        assert assembled["rollup"]["pages_inconclusive"] == 1
        dead = assembled["locations"][3]
        assert dead["url"] == "https://example.com/down"
        assert dead["status"] == "inconclusive"
        assert dead["score"] is None

        # denominator stays at 3 graded pages -> C-01 on 2 is still sitewide
        assert [e["check_id"] for e in assembled["sitewide"]] == ["C-01"]
        assert assembled["sitewide"][0]["pages_affected"] == 2

        with db.connect(autocommit=True) as c:
            grow = c.execute("select * from audits where id=%s", (group_id,)).fetchone()
        assert grow["gate_state"] == "group_rollup"
        assert len(grow["scores"]["member_audit_ids"]) == 4
