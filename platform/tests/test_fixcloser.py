"""Tests for gm.content.engine_port + gm.content.fixcloser.

The request builder is verified against a RECORDED copy of the zod-required
fields from the serp-analyzer repo (see RECORDED_ZOD_REQUIREMENTS below —
hand-maintained, with the source pointer). Transport tests use
httpx.MockTransport; the end-to-end job-flow test runs under the
DATABASE_URL skip guard with a fake engine and a fake run_draft_audit
(agent B's function, monkeypatched onto gm.audit.pipeline). ZERO network.
"""

import copy
import json
import os
import re
import types
import uuid

import httpx
import pytest
from psycopg.types.json import Jsonb

from gm import db
from gm.audit.registry import Registry
from gm.content import fixcloser
from gm.content.engine_port import (
    AUTHOR_MISSING_MSG,
    ContentEngine,
    EngineUnavailable,
    RequestFieldMissing,
    build_writer_request,
)

# ---------------------------------------------------------------------------
# RECORDED zod requirements — hand-maintained copy of the required fields in
# '/Users/arunsharma/Documents/New project/serp-analyzer/src/blog/types.ts'
# (BlogWriterRequestSchema incl. the enforce_human_signals superRefine),
# recorded 2026-07-04. If that schema changes, update this block AND the
# builder in gm/content/engine_port.py together.
# ---------------------------------------------------------------------------

INTENTS = {"informational", "commercial", "transactional", "navigational"}
VISUAL_TYPES = {"screenshot", "diagram", "chart", "framework", "product_ui"}
AUTHORITY_TIERS = {"primary", "industry", "editorial"}


def _nonempty(value):
    return isinstance(value, str) and len(value) >= 1


def _url_ok(value):
    return isinstance(value, str) and re.match(r"^https?://\S+", value)


def assert_schema_valid(req: dict) -> None:
    """Mirror of the recorded zod constraints (types.ts). Assert-based so a
    failure names the violated field."""
    assert _nonempty(req.get("topic")), "topic: min 1"
    assert _nonempty(req.get("primary_keyword")), "primary_keyword: min 1"
    if "search_intent" in req:
        assert req["search_intent"] in INTENTS, "search_intent: enum"
    brand = req.get("brand") or {}
    assert _nonempty(brand.get("name")), "brand.name: min 1"
    assert _nonempty(brand.get("domain")), "brand.domain: min 1"
    assert _nonempty(brand.get("product_description")), "brand.product_description: min 1"
    sources = req.get("sources") or []
    assert len(sources) >= 1, "sources: min 1"
    for s in sources:
        assert _nonempty(s.get("id")), "source.id: min 1"
        assert _nonempty(s.get("title")), "source.title: min 1"
        assert _url_ok(s.get("url")), "source.url: valid URL"
        assert _nonempty(s.get("excerpt")), "source.excerpt: min 1"
        assert s.get("authority_tier", "editorial") in AUTHORITY_TIERS
    if "article" in req:
        twc = req["article"].get("target_word_count", 1800)
        assert isinstance(twc, int) and 800 <= twc <= 4000, "article.target_word_count band"
    # ── superRefine block (enforce_human_signals=true) ──────────────────
    assert req.get("enforce_human_signals") is True, "convergence fix: must be True"
    author = req.get("author")
    assert isinstance(author, dict), "author: required"
    assert _nonempty(author.get("name")), "author.name: min 1"
    assert _nonempty(author.get("title")), "author.title: min 1"
    assert isinstance(author.get("bio"), str) and len(author["bio"]) >= 30, "author.bio: min 30"
    assert _url_ok(author.get("linkedin_url")), "author.linkedin_url: valid URL"
    assert re.search(r"linkedin\.com", author["linkedin_url"], re.I), "linkedin.com refine"
    fpd = req.get("first_party_data") or []
    assert len(fpd) >= 1, "first_party_data: min 1"
    for f in fpd:
        assert isinstance(f.get("finding"), str) and len(f["finding"]) >= 20
        assert _nonempty(f.get("metric")), "first_party.metric: min 1"
        assert _nonempty(f.get("source_description")), "first_party.source_description: min 1"
    examples = req.get("named_examples") or []
    assert len(examples) >= 3, "named_examples: min 3"
    for e in examples:
        assert _nonempty(e.get("brand")), "named_example.brand: min 1"
        assert isinstance(e.get("observation"), str) and len(e["observation"]) >= 20
    stance = req.get("editorial_stance")
    assert isinstance(stance, dict), "editorial_stance: required"
    assert isinstance(stance.get("claim"), str) and len(stance["claim"]) >= 20
    assert (isinstance(stance.get("supporting_reasoning"), str)
            and len(stance["supporting_reasoning"]) >= 20)
    visuals = req.get("original_visuals") or []
    assert len(visuals) >= 1, "original_visuals: min 1"
    for v in visuals:
        assert v.get("type") in VISUAL_TYPES, "visual.type: enum"
        assert _nonempty(v.get("placement_hint")), "visual.placement_hint: min 1"
        assert isinstance(v.get("description"), str) and len(v["description"]) >= 20
    primary = sum(1 for s in sources if s.get("authority_tier") == "primary")
    assert primary >= 3, "sources: min 3 primary authority-tier"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

QUERY = "best med spa dubai"
CLIENT_PAGE = "https://client.example/med-spa"

SITE = {
    "domain_norm": "client.example",
    "brand_terms": ["Client Spa"],
    "notes": "Dubai med-spa offering laser, skin and injectable treatments.",
    "author": {
        "name": "Dr. Amina Khan",
        "title": "Medical Director",
        "bio": "Board-certified dermatologist with 12 years of clinical practice in Dubai.",
        "sameAs": ["https://www.linkedin.com/in/amina-khan", "https://x.com/aminakhan"],
    },
    "first_party": [
        {"fact": "Our clinic performed 1,200 laser sessions in 2025 with a 4.9/5 rating.",
         "source": "Clinic booking system, 2025"},
    ],
}

CITATION = {
    "title": "HTTPS as a ranking signal", "source_org": "Google",
    "source_url": "https://developers.google.com/search/blog/https-ranking",
}

SERP_TABLE = [
    {"rank": 1, "url": "https://competitor-a.com/med-spa", "domain": "competitor-a.com",
     "title": "Best Med Spas in Dubai | A", "type": "organic", "score": 88.0},
    {"rank": 2, "url": CLIENT_PAGE, "domain": "client.example",
     "title": "Med Spa | Client", "type": "organic"},
    {"rank": 3, "url": "https://competitor-b.com/guide", "domain": "competitor-b.com",
     "title": "Dubai Med Spa Guide", "type": "organic"},
    {"rank": 4, "url": "https://competitor-c.com/spa", "domain": "competitor-c.com",
     "title": "Med Spa Dubai Prices", "type": "organic"},
]

BRIEF_ROW = {
    "target": {"query": QUERY, "page": CLIENT_PAGE, "kind": "refresh"},
    "brief": {
        "query_norm": QUERY,
        "serp_table": SERP_TABLE,
        "paa": [
            {"question": "How much does a med spa cost?", "volume": 320},
            {"question": "Is a med spa worth it?", "volume": None},
        ],
        "gaps": [
            {"check_id": "E-01", "name": "Answer-First Structure",
             "client_status": "fail", "competitors_passing": 2},
        ],
        "required_fixes": [
            {"check_id": "A-01", "name": "HTTPS Enforcement", "status": "fail",
             "citations": [CITATION]},
        ],
        "synthesis": {
            "angle": "The only guide that prices every treatment upfront.",
            "title": "Med Spa Dubai: Treatments, Prices, How to Choose",
        },
        "notes": [],
    },
}

ENGINE_RESPONSE = {
    "blog_package": {
        "article": {"title": "Med Spa Dubai", "slug": "best-med-spa-dubai"},
        "html": "<article><h1>Med Spa Dubai</h1></article>",
        "json_ld": {"@type": "Article"},
        "validation": {
            "warnings": [],
            "uncited_source_ids": [],
            "human_signal_gaps": ["reviewer entity missing"],
            "pending_visual_placements": ["visual-1: comparison chart placeholder"],
        },
        "editorial_checklist": [
            {"id": "EC1", "label": "Author entity present", "pass": True, "detail": ""},
            {"id": "EC5", "label": "Original visual embedded", "pass": False,
             "detail": "placeholder only"},
        ],
        "publish_ready": False,
    },
    "audit": {
        "summary": {"overall_score": 82, "rating": "Minor fixes needed"},
        "fix_summary": [
            {"check_id": "B2", "section_id": "B", "issue": "Meta description too long",
             "exact_fix": "Trim to 155 characters."},
        ],
    },
    "iterations": [],
    "total_cost_usd": 0.42,
}


def brief_variant(**brief_overrides) -> dict:
    row = copy.deepcopy(BRIEF_ROW)
    row["brief"].update(brief_overrides)
    return row


def site_variant(**overrides) -> dict:
    site = copy.deepcopy(SITE)
    site.update(overrides)
    return site


def make_check(check_id: str, name: str) -> dict:
    return {"check_id": check_id, "check_version": 1, "name": name, "weight": 1,
            "severity": "high", "badge": "static_rule", "sources": []}


def mini_registry() -> Registry:
    return Registry(version="vtest", checks={
        "A-01": make_check("A-01", "HTTPS Enforcement"),
        "A-02": make_check("A-02", "Title Tag Length"),
    })


# ---------------------------------------------------------------------------
# Request builder — schema validity + grounding
# ---------------------------------------------------------------------------

class TestBuildWriterRequest:
    def test_schema_valid_against_recorded_zod_requirements(self):
        req = build_writer_request(SITE, BRIEF_ROW, kind="refresh")
        assert_schema_valid(req)

    def test_field_mapping(self):
        req = build_writer_request(SITE, BRIEF_ROW, kind="refresh")
        assert req["topic"] == "Med Spa Dubai: Treatments, Prices, How to Choose"
        assert req["primary_keyword"] == QUERY
        # PAA -> secondary_keywords (no separate questions field in the schema)
        assert req["secondary_keywords"] == [
            "How much does a med spa cost?", "Is a med spa worth it?",
        ]
        assert req["search_intent"] == "informational"
        assert req["angle"].startswith("The only guide")
        assert req["brand"] == {
            "name": "Client Spa", "domain": "client.example",
            "product_description": SITE["notes"],
        }
        assert req["article"]["slug"] == "med-spa"  # refresh keeps the page slug
        assert req["article"]["target_word_count"] >= 1400  # diagnosis word-count band
        assert req["article"]["author_name"] == "Dr. Amina Khan"

    def test_author_entity_from_sites_author(self):
        req = build_writer_request(SITE, BRIEF_ROW, kind="refresh")
        author = req["author"]
        assert author["name"] == "Dr. Amina Khan"
        assert author["title"] == "Medical Director"
        assert author["linkedin_url"] == "https://www.linkedin.com/in/amina-khan"
        assert author["twitter_url"] == "https://x.com/aminakhan"
        assert len(author["bio"]) >= 30

    def test_first_party_from_sites_never_invented(self):
        req = build_writer_request(SITE, BRIEF_ROW, kind="refresh")
        [fpd] = req["first_party_data"]
        assert fpd["finding"] == SITE["first_party"][0]["fact"]
        assert fpd["source_description"] == "Clinic booking system, 2025"
        assert fpd["metric"] == "1,200"  # extracted from the fact, not invented

    def test_sources_citations_plus_serp_client_excluded(self):
        req = build_writer_request(SITE, BRIEF_ROW, kind="refresh")
        urls = [s["url"] for s in req["sources"]]
        assert CITATION["source_url"] in urls  # registry citation first
        assert CLIENT_PAGE not in urls         # never your own page as a source
        assert len(urls) == 4                  # 1 citation + 3 competitor rows
        assert all(s["authority_tier"] == "primary" for s in req["sources"])
        assert req["sources"][0]["publisher"] == "Google"

    def test_named_examples_are_real_serp_rows(self):
        req = build_writer_request(SITE, BRIEF_ROW, kind="refresh")
        brands = [e["brand"] for e in req["named_examples"]]
        assert brands == ["competitor-a.com", "competitor-b.com", "competitor-c.com"]
        assert req["named_examples"][0]["metric"] == "Google rank #1"
        assert QUERY in req["named_examples"][0]["observation"]

    def test_stance_from_synthesis_angle(self):
        req = build_writer_request(SITE, BRIEF_ROW, kind="refresh")
        assert req["editorial_stance"]["claim"].startswith("The only guide")

    def test_stance_falls_back_to_gap_data(self):
        req = build_writer_request(SITE, brief_variant(synthesis=None), kind="refresh")
        assert "Answer-First Structure" in req["editorial_stance"]["claim"]
        assert "2" in req["editorial_stance"]["supporting_reasoning"]
        assert req["topic"] == QUERY  # no synthesis title -> query
        assert_schema_valid(req)

    def test_visual_spec_from_serp_data(self):
        req = build_writer_request(SITE, BRIEF_ROW, kind="refresh")
        [visual] = req["original_visuals"]
        assert visual["type"] == "chart"
        assert QUERY in visual["description"]

    def test_enforce_human_signals_always_true(self):
        # THE convergence fix (docs/convergence-diagnosis.md §3.1)
        req = build_writer_request(SITE, BRIEF_ROW, kind="refresh")
        assert req["enforce_human_signals"] is True


class TestBuildFailFast:
    def test_empty_author_uses_contract_message(self):
        with pytest.raises(RequestFieldMissing) as err:
            build_writer_request(site_variant(author={}), BRIEF_ROW, kind="refresh")
        assert AUTHOR_MISSING_MSG in str(err.value)
        assert "set author first: gm site set-author" in str(err.value)

    def test_author_without_linkedin(self):
        site = site_variant(author={"name": "A", "title": "T",
                                    "bio": "x" * 40, "sameAs": ["https://x.com/a"]})
        with pytest.raises(RequestFieldMissing, match="author.linkedin_url"):
            build_writer_request(site, BRIEF_ROW, kind="refresh")

    def test_author_bio_too_short(self):
        site = site_variant(author={"name": "A", "title": "T", "bio": "short",
                                    "sameAs": ["https://linkedin.com/in/a"]})
        with pytest.raises(RequestFieldMissing, match="author.bio"):
            build_writer_request(site, BRIEF_ROW, kind="refresh")

    def test_no_first_party_data(self):
        with pytest.raises(RequestFieldMissing, match="first_party_data"):
            build_writer_request(site_variant(first_party=[]), BRIEF_ROW, kind="refresh")

    def test_too_few_named_examples(self):
        # 2 competitor rows: sources still reach 3 primary (1 citation + 2
        # SERP), but named_examples cannot reach 3 — the named-field error.
        row = brief_variant(serp_table=SERP_TABLE[:3])
        with pytest.raises(RequestFieldMissing, match="named_examples"):
            build_writer_request(SITE, row, kind="refresh")

    def test_too_few_primary_sources(self):
        row = brief_variant(serp_table=[], required_fixes=[])
        with pytest.raises(RequestFieldMissing, match="sources|named_examples"):
            build_writer_request(SITE, row, kind="refresh")

    def test_missing_product_description(self):
        with pytest.raises(RequestFieldMissing, match="brand.product_description"):
            build_writer_request(site_variant(notes=None), BRIEF_ROW, kind="refresh")

    def test_no_grounding_for_stance(self):
        row = brief_variant(synthesis=None, gaps=[])
        with pytest.raises(RequestFieldMissing, match="editorial_stance"):
            build_writer_request(SITE, row, kind="refresh")


# ---------------------------------------------------------------------------
# ContentEngine transport (httpx.MockTransport — zero network)
# ---------------------------------------------------------------------------

def fake_engine(handler, token=None) -> ContentEngine:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ContentEngine(base_url="http://engine.test", token=token, client=client)


class TestContentEngine:
    def test_missing_env_is_engine_unavailable(self, monkeypatch):
        monkeypatch.delenv("CONTENT_ENGINE_URL", raising=False)
        with pytest.raises(EngineUnavailable, match="CONTENT_ENGINE_URL"):
            ContentEngine()

    def test_success_posts_request_with_bearer(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=ENGINE_RESPONSE)

        engine = fake_engine(handler, token="tkn")
        result = engine.write_and_audit({"topic": "x", "enforce_human_signals": True})
        assert result["audit"]["summary"]["overall_score"] == 82
        assert seen["url"] == "http://engine.test/blog/write-and-audit"
        assert seen["auth"] == "Bearer tkn"
        assert seen["body"]["enforce_human_signals"] is True

    def test_connect_error_is_engine_unavailable(self):
        def handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        with pytest.raises(EngineUnavailable, match="unreachable"):
            fake_engine(handler).write_and_audit({})

    def test_5xx_is_engine_unavailable(self):
        engine = fake_engine(lambda r: httpx.Response(503, text="down"))
        with pytest.raises(EngineUnavailable, match="503"):
            engine.write_and_audit({})

    def test_400_is_our_schema_bug_not_engine_down(self):
        payload = {"error": "Invalid input", "details": {"fieldErrors": {"author": ["req"]}}}
        engine = fake_engine(lambda r: httpx.Response(400, json=payload))
        with pytest.raises(ValueError, match="400.*author") as err:
            engine.write_and_audit({})
        assert not isinstance(err.value, EngineUnavailable)

    def test_non_json_is_engine_unavailable(self):
        engine = fake_engine(lambda r: httpx.Response(200, text="<html>proxy</html>"))
        with pytest.raises(EngineUnavailable, match="non-JSON"):
            engine.write_and_audit({})


# ---------------------------------------------------------------------------
# Pure fixcloser helpers
# ---------------------------------------------------------------------------

class TestOpenItems:
    def test_merges_audit_and_validation_items(self):
        todos = fixcloser.extract_open_items(ENGINE_RESPONSE)
        texts = [t["todo"] for t in todos]
        assert "Meta description too long" in texts
        assert "reviewer entity missing" in texts
        assert any("comparison chart placeholder" in t for t in texts)
        assert any("Original visual embedded" in t for t in texts)  # failed checklist
        assert not any("Author entity present" in t for t in texts)  # passing item skipped
        assert todos[0]["fix"] == "Trim to 155 characters."

    def test_tolerates_missing_sections(self):
        assert fixcloser.extract_open_items({}) == []

    def test_cost_estimate(self):
        assert fixcloser.estimate_cost_cents(ENGINE_RESPONSE) == pytest.approx(42.0)
        assert fixcloser.estimate_cost_cents({"totalCostUsd": 0.1}) == pytest.approx(10.0)
        assert fixcloser.estimate_cost_cents({}) == 0.0

    def test_our_failing_todos_use_registry_names(self):
        rows = [{"check_id": "A-01", "status": "fail"}, {"check_id": "Z-99", "status": "warn"}]
        todos = fixcloser.our_failing_todos(rows, mini_registry())
        assert todos[0]["todo"] == "HTTPS Enforcement (A-01) — fail in the draft scorecard audit"
        assert todos[1]["todo"].startswith("Z-99 (Z-99) — warn")  # unknown id falls back


# ---------------------------------------------------------------------------
# End-to-end job flow (DB required; fake engine + fake draft audit)
# ---------------------------------------------------------------------------

db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)


@pytest.fixture(scope="session")
def _migrated():
    db.run_migrations()


@pytest.fixture
def org_site(_migrated):
    with db.connect(autocommit=True) as c:
        org = c.execute(
            "insert into orgs (name) values ('fixcloser-test') returning id"
        ).fetchone()["id"]
        site = c.execute(
            "insert into sites (org_id, domain_norm, brand_terms, notes, author, first_party)"
            " values (%s, %s, %s, %s, %s, %s) returning id",
            (org, f"client-{uuid.uuid4().hex[:10]}.example", ["Client Spa"], SITE["notes"],
             Jsonb(SITE["author"]), Jsonb(SITE["first_party"])),
        ).fetchone()["id"]
    return str(org), str(site)


def make_loop_rows(org, site, kind="refresh"):
    """briefs row + content_items row for one trip around the loop."""
    with db.connect(autocommit=True) as c:
        brief = c.execute(
            "insert into briefs (org_id, site_id, target, brief) values (%s, %s, %s, %s)"
            " returning id",
            (org, site, Jsonb(BRIEF_ROW["target"]), Jsonb(BRIEF_ROW["brief"])),
        ).fetchone()["id"]
        item = c.execute(
            "insert into content_items (org_id, site_id, brief_id, kind)"
            " values (%s, %s, %s, %s) returning id",
            (org, site, brief, kind),
        ).fetchone()["id"]
    return str(item)


def install_fake_draft_audit(monkeypatch, *, fail=False):
    """Agent B's run_draft_audit is built concurrently: fake it to its
    wave-3 contract signature, writing a real audits row + findings."""
    import gm.audit.pipeline as pipeline

    calls: dict = {}

    def fake_run_draft_audit(conn, *, org_id, site_id, draft_html, url_hint, llm,
                             registry=None, cost_cap_cents=150.0, draft_id=None):
        if fail:
            raise RuntimeError("draft audit blew up")
        calls.update(draft_html=draft_html, url_hint=url_hint, draft_id=draft_id,
                     cost_cap_cents=cost_cap_cents)
        audit_id = conn.execute(
            "insert into audits (org_id, site_id, draft_id, url, registry_version,"
            " status, gate_state) values (%s, %s, %s, %s, 'vtest', 'done', 'draft')"
            " returning id",
            (org_id, site_id, draft_id, url_hint),
        ).fetchone()["id"]
        for check_id, status in [("A-01", "fail"), ("A-02", "warn"), ("E-01", "pass")]:
            conn.execute(
                "insert into audit_findings (org_id, audit_id, check_id, check_version,"
                " status, badge) values (%s, %s, %s, 1, %s, 'static_rule')",
                (org_id, audit_id, check_id, status),
            )
        return str(audit_id)

    monkeypatch.setattr(pipeline, "run_draft_audit", fake_run_draft_audit, raising=False)
    return calls


def make_ctx(conn, org, site, payload):
    job = types.SimpleNamespace(id=101, payload=payload, org_id=org, site_id=site)
    return types.SimpleNamespace(conn=conn, job=job)


def run_handler(org, site, item_id, engine, **kwargs):
    with db.connect() as conn:
        db.set_org(conn, org)
        ctx = make_ctx(conn, org, site, {"content_item_id": item_id})
        fixcloser.handle_close_fixes(ctx, engine=engine, llm=object(),
                                     registry=mini_registry(), **kwargs)
        conn.commit()


def fetch_state(item_id):
    with db.connect(autocommit=True) as c:
        drafts = c.execute(
            "select * from drafts where content_item_id = %s order by version", (item_id,)
        ).fetchall()
        item = c.execute(
            "select * from content_items where id = %s", (item_id,)
        ).fetchone()
    return item, drafts


@db_required
class TestCloseFixesJobFlow:
    def test_full_flow(self, monkeypatch, org_site):
        org, site = org_site
        item_id = make_loop_rows(org, site)
        calls = install_fake_draft_audit(monkeypatch)
        posted = {}

        def handler(request):
            posted["body"] = json.loads(request.content)
            return httpx.Response(200, json=ENGINE_RESPONSE)

        run_handler(org, site, item_id, fake_engine(handler))
        item, drafts = fetch_state(item_id)

        # the engine saw a schema-valid, convergence-fixed request
        assert_schema_valid(posted["body"])
        assert posted["body"]["author"]["name"] == "Dr. Amina Khan"

        # drafts row: version=next, package, cost from their response
        [draft] = drafts
        assert draft["version"] == 1
        assert draft["package"]["audit"]["summary"]["overall_score"] == 82
        assert float(draft["cost_cents"]) == pytest.approx(42.0)

        # draft audit ran on the returned HTML and is linked back
        assert str(draft["scorecard_audit_id"])
        assert calls["draft_html"] == ENGINE_RESPONSE["blog_package"]["html"]
        assert calls["url_hint"] == CLIENT_PAGE  # refresh targets the existing page
        assert str(calls["draft_id"]) == str(draft["id"])

        # human_todos = engine open items + OUR failing check names
        todos = [t["todo"] for t in draft["human_todos"]]
        assert "Meta description too long" in todos
        assert "reviewer entity missing" in todos
        assert "HTTPS Enforcement (A-01) — fail in the draft scorecard audit" in todos
        assert "Title Tag Length (A-02) — warn in the draft scorecard audit" in todos
        assert not any("E-01" in t for t in todos)  # passing check is not a todo

        assert item["status"] == "review"

        # engine cost recorded as a cost event
        with db.connect(autocommit=True) as c:
            cost = c.execute(
                "select cost_cents from cost_events where org_id = %s"
                " and purpose = 'close_fixes_write'", (org,),
            ).fetchone()
        assert float(cost["cost_cents"]) == pytest.approx(42.0)

    def test_second_run_gets_next_version(self, monkeypatch, org_site):
        org, site = org_site
        item_id = make_loop_rows(org, site)
        install_fake_draft_audit(monkeypatch)
        engine = fake_engine(lambda r: httpx.Response(200, json=ENGINE_RESPONSE))

        run_handler(org, site, item_id, engine)
        run_handler(org, site, item_id, engine)
        _, drafts = fetch_state(item_id)
        assert [d["version"] for d in drafts] == [1, 2]

    def test_engine_down_fails_honestly_without_side_effects(self, monkeypatch, org_site):
        org, site = org_site
        item_id = make_loop_rows(org, site)
        install_fake_draft_audit(monkeypatch)

        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(EngineUnavailable):
            run_handler(org, site, item_id, fake_engine(handler))
        item, drafts = fetch_state(item_id)
        assert drafts == []
        assert item["status"] == "briefed"  # untouched — retryable by re-enqueue

    def test_empty_author_fails_fast_before_the_engine(self, monkeypatch, org_site):
        org, _ = org_site
        with db.connect(autocommit=True) as c:
            bare_site = c.execute(
                "insert into sites (org_id, domain_norm, notes) values (%s, %s, %s)"
                " returning id",
                (org, f"bare-{uuid.uuid4().hex[:10]}.example", "notes"),
            ).fetchone()["id"]
        item_id = make_loop_rows(org, str(bare_site))
        install_fake_draft_audit(monkeypatch)

        def handler(request):  # pragma: no cover — must never be reached
            raise AssertionError("engine must not be called without an author")

        with pytest.raises(RequestFieldMissing, match="set author first: gm site set-author"):
            run_handler(org, str(bare_site), item_id, fake_engine(handler))

    def test_missing_payload_field(self, org_site):
        org, site = org_site
        with db.connect() as conn:
            db.set_org(conn, org)
            ctx = make_ctx(conn, org, site, {})
            with pytest.raises(ValueError, match="content_item_id"):
                fixcloser.handle_close_fixes(ctx, engine=None, llm=object())
