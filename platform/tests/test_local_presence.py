"""Tests for the local-presence check family (Phase D3, WP-F).

Pure tests (serp local-pack entry retention, you-match, the full J-05..J-07
status matrix incl. every inconclusive branch, median determinism, registry
golden, classifier-exclusion proof) run without a DB. DB tests plant
tracked_queries + serp_snapshots fixtures and run under the DATABASE_URL skip
guard. ZERO network; the classifier is a FakeLlm; migration 011's
queue_items kind='local_presence' is ensured defensively in test setup.
"""

import json
import os
import re
import socket
import uuid
from dataclasses import dataclass, field

import pytest
from psycopg.types.json import Jsonb

from gm import db
from gm.audit import pipeline, safety
from gm.audit.fetch import FetchResult
from gm.audit.registry import VALID_FIX_TYPES, load_registry
from gm.intel import local_presence, serp
from gm.intel.serp import _normalize_items

needs_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

LP_IDS = ("J-05", "J-06", "J-07")


# ---------------------------------------------------------------------------
# helpers: pack/entry builders (pure)
# ---------------------------------------------------------------------------


def entry(**fields) -> dict:
    return dict(fields)


YOU_FULL = entry(
    rank=1, title="Acme Clinic", domain="example.com", url="https://example.com/",
    phone="+971 4 123 4567", rating=4.8, votes=200,
)


def sighting(query="best clinic dubai", you=None, others=(), fetched_at=1000):
    entries = list(others)
    if you is not None:
        entries = [you, *entries]
    return {"query_norm": query, "fetched_at": fetched_at, "entries": entries, "you": you}


def pack_of(*sightings, packs_unobserved=0, note=None):
    ss = sorted(sightings, key=lambda s: s["fetched_at"], reverse=True)
    return {
        "sightings": ss,
        "packs_seen": len(ss),
        "packs_unobserved": packs_unobserved,
        "you_seen": sum(1 for s in ss if s["you"] is not None),
        "note": note,
    }


SITE = {"domain_norm": "example.com", "brand_terms": ["Acme Clinic"]}


def evaluate(pack, site=SITE):
    return local_presence.evaluate_local_presence(pack, site)


# ---------------------------------------------------------------------------
# serp._normalize_items: local-pack entry retention (new vs legacy shapes)
# ---------------------------------------------------------------------------


class TestEntryRetention:
    def test_entries_retained_in_order_with_all_fields(self):
        items = [
            {"type": "organic", "rank_group": 1, "url": "https://a.com/x",
             "domain": "a.com", "title": "A", "description": "d"},
            {"type": "local_pack", "rank_group": 1, "title": "Acme Clinic",
             "domain": "example.com", "url": "https://example.com/", "phone": "+971 4 1",
             "rating": {"rating_type": "Max5", "value": 4.8, "votes_count": 200},
             "is_paid": False},
            {"type": "local_pack", "rank_group": 2, "title": "Rival Spa",
             "rating": {"value": 4, "votes_count": 90}, "is_paid": True},
        ]
        organic, features = _normalize_items(items)
        assert [e["url"] for e in organic] == ["https://a.com/x"]
        packs = [f for f in features if f["type"] == "local_pack"]
        assert len(packs) == 1  # one feature entry per type, entries folded in
        entries = packs[0]["entries"]
        assert entries[0] == {
            "rank": 1, "title": "Acme Clinic", "domain": "example.com",
            "url": "https://example.com/", "phone": "+971 4 1",
            "rating": 4.8, "votes": 200, "is_paid": False,
        }
        assert entries[1] == {
            "rank": 2, "title": "Rival Spa", "rating": 4.0, "votes": 90, "is_paid": True,
        }
        assert isinstance(entries[1]["rating"], float)
        assert isinstance(entries[1]["votes"], int)

    def test_fields_only_when_provider_sent_them(self):
        _, features = _normalize_items([{"type": "local_pack", "title": "Bare"}])
        (feat,) = features
        assert feat["entries"] == [{"title": "Bare"}]  # no planted defaults

    def test_malformed_fields_are_dropped_not_guessed(self):
        _, features = _normalize_items([
            {"type": "local_pack", "rank_group": True, "title": "  ", "phone": None,
             "rating": "4.5", "is_paid": "yes"},
        ])
        assert features[0]["entries"] == [{}]

    def test_other_feature_types_gain_no_entries_key(self):
        _, features = _normalize_items([
            {"type": "featured_snippet", "url": "https://a.com/x", "domain": "a.com"},
            {"type": "local_pack", "title": "Acme"},
        ])
        by_type = {f["type"]: f for f in features}
        assert "entries" not in by_type["featured_snippet"]
        assert "entries" in by_type["local_pack"]


# ---------------------------------------------------------------------------
# you-match matrix (pure)
# ---------------------------------------------------------------------------


class TestYouMatch:
    BRANDS = {"acme clinic"}

    def match(self, e, brands=None, host="example.com"):
        return local_presence._entry_is_you(e, brands or self.BRANDS, host)

    def test_brand_term_case_and_whitespace_folded(self):
        assert self.match(entry(title="ACME   Clinic"))
        assert self.match(entry(title="acme clinic"))
        assert not self.match(entry(title="Acme Clinic Dubai"))  # not an exact folded term

    def test_domain_match_subdomain_aware(self):
        assert self.match(entry(domain="example.com"))
        assert self.match(entry(domain="Maps.Example.com".lower()))
        assert self.match(entry(url="https://www.example.com/place"))  # www stripped

    def test_url_host_fallback_when_no_domain(self):
        assert self.match(entry(url="https://booking.example.com/x"))

    def test_foreign_host_and_foreign_title_do_not_match(self):
        assert not self.match(entry(title="Rival Spa", domain="rival.com"))
        assert not self.match(entry(domain="notexample.com"))  # suffix != subdomain
        assert not self.match(entry())


# ---------------------------------------------------------------------------
# J-05 — GBP presence & completeness (full matrix)
# ---------------------------------------------------------------------------


class TestJ05:
    def test_pass_newest_entry_complete(self):
        out = evaluate(pack_of(sighting(you=YOU_FULL)))["J-05"]
        assert out["status"] == "pass"
        assert out["source"] == "deterministic"
        assert "1 of 1 tracked query" in out["note"]

    def test_warn_core_field_absent_lists_missing(self):
        you = entry(title="Acme Clinic", domain="example.com", rating=4.8, votes=10)
        out = evaluate(pack_of(sighting(you=you)))["J-05"]
        assert out["status"] == "warn"
        assert "phone" in out["note"] and "url" in out["note"]

    def test_newest_sighting_decides_completeness(self):
        old_you = entry(title="Acme Clinic", domain="example.com")  # incomplete, older
        new = sighting(query="q new", you=YOU_FULL, fetched_at=2000)
        old = sighting(query="q old", you=old_you, fetched_at=1000)
        assert evaluate(pack_of(old, new))["J-05"]["status"] == "pass"

    def test_fail_packs_observed_but_you_absent(self):
        rival = entry(title="Rival Spa", domain="rival.com", rating=4.0, votes=50)
        out = evaluate(pack_of(sighting(you=None, others=[rival])))["J-05"]
        assert out["status"] == "fail"
        assert "you appear in none" in out["note"]

    def test_inconclusive_no_pack_observed(self):
        out = evaluate(pack_of(note="no local-pack sighting for tracked queries"))["J-05"]
        assert out["status"] == "inconclusive"
        assert "no local-pack sighting" in out["note"]

    def test_inconclusive_only_legacy_no_entry_snapshots(self):
        pack = pack_of(packs_unobserved=3, note="local pack present for 3 tracked queries"
                       " but entries unobserved (snapshots predate entry retention)")
        out = evaluate(pack)["J-05"]
        assert out["status"] == "inconclusive"
        assert "entries unobserved" in out["note"]


# ---------------------------------------------------------------------------
# J-06 — NAP consistency (full matrix)
# ---------------------------------------------------------------------------


def _you(title="Acme Clinic", phone="+971 4 123", domain="example.com", **kw):
    return entry(title=title, phone=phone, domain=domain, **kw)


class TestJ06:
    def test_pass_one_identity_domain_matches(self):
        pack = pack_of(sighting(query="a", you=_you(), fetched_at=2),
                       sighting(query="b", you=_you(), fetched_at=1))
        out = evaluate(pack)["J-06"]
        assert out["status"] == "pass"
        assert "2 sightings" in out["note"] and "example.com" in out["note"]

    def test_warn_case_only_title_variant(self):
        pack = pack_of(sighting(query="a", you=_you(title="ACME CLINIC"), fetched_at=2),
                       sighting(query="b", you=_you(), fetched_at=1))
        out = evaluate(pack)["J-06"]
        assert out["status"] == "warn"
        assert "case/whitespace" in out["note"]

    def test_warn_whitespace_only_phone_variant(self):
        pack = pack_of(sighting(query="a", you=_you(phone="+9714123"), fetched_at=2),
                       sighting(query="b", you=_you(phone="+971 4 123"), fetched_at=1))
        assert evaluate(pack)["J-06"]["status"] == "warn"

    def test_fail_conflicting_phone(self):
        pack = pack_of(sighting(query="a", you=_you(phone="+971 4 999"), fetched_at=2),
                       sighting(query="b", you=_you(phone="+971 4 123"), fetched_at=1))
        out = evaluate(pack)["J-06"]
        assert out["status"] == "fail"
        assert "phones" in out["note"]

    def test_fail_conflicting_name(self):
        pack = pack_of(sighting(query="a", you=_you(title="Acme Medical Center"), fetched_at=2),
                       sighting(query="b", you=_you(), fetched_at=1))
        out = evaluate(pack)["J-06"]
        assert out["status"] == "fail"
        assert "names" in out["note"]

    def test_fail_foreign_domain_even_when_brand_matched(self):
        # attributed via brand term, but the listing points somewhere else
        pack = pack_of(sighting(query="a", you=_you(domain="acme-directory.net"), fetched_at=2),
                       sighting(query="b", you=_you(), fetched_at=1))
        out = evaluate(pack)["J-06"]
        assert out["status"] == "fail"
        assert "acme-directory.net" in out["note"]

    def test_inconclusive_below_two_attributable_sightings(self):
        rival = entry(title="Rival", domain="rival.com")
        zero = pack_of(sighting(you=None, others=[rival]))
        one = pack_of(sighting(query="a", you=_you(), fetched_at=2),
                      sighting(query="b", you=None, others=[rival], fetched_at=1))
        for pack, n in ((zero, 0), (one, 1)):
            out = evaluate(pack)["J-06"]
            assert out["status"] == "inconclusive"
            assert str(n) in out["note"]


# ---------------------------------------------------------------------------
# J-07 — review signal vs local pack (full matrix + median determinism)
# ---------------------------------------------------------------------------


def _rated(rating, votes, name="Rival"):
    return entry(title=name, domain="rival.com", rating=rating, votes=votes)


class TestJ07:
    def pack(self, you, others):
        return pack_of(sighting(you=you, others=others))

    def test_pass_at_or_above_both_medians(self):
        # even-count medians: ratings (4.4, 4.6) -> 4.5; votes (100, 150) -> 125
        you = entry(title="Acme Clinic", domain="example.com", rating=4.5, votes=125)
        out = evaluate(self.pack(you, [_rated(4.6, 100), _rated(4.4, 150)]))["J-07"]
        assert out["status"] == "pass"
        assert "4.5" in out["note"] and "125" in out["note"]

    def test_warn_below_one_median(self):
        you = entry(title="Acme", domain="example.com", rating=4.4, votes=200)
        out = evaluate(self.pack(you, [_rated(4.6, 100), _rated(4.4, 150)]))["J-07"]
        assert out["status"] == "warn"

    def test_fail_rating_more_than_half_point_below_median(self):
        you = entry(title="Acme", domain="example.com", rating=3.9, votes=200)
        out = evaluate(self.pack(you, [_rated(4.6, 100), _rated(4.4, 150)]))["J-07"]
        assert out["status"] == "fail"  # 3.9 < 4.5 - 0.5

    def test_fail_votes_below_half_median(self):
        you = entry(title="Acme", domain="example.com", rating=4.6, votes=62)
        out = evaluate(self.pack(you, [_rated(4.6, 100), _rated(4.4, 150)]))["J-07"]
        assert out["status"] == "fail"  # 62 < 125/2

    def test_boundary_exactly_half_votes_is_warn_not_fail(self):
        you = entry(title="Acme", domain="example.com", rating=4.6, votes=50)
        out = evaluate(self.pack(you, [_rated(4.5, 100)]))["J-07"]
        assert out["status"] == "warn"  # 50 == 100*0.5 -> not < half

    def test_inconclusive_your_rating_or_votes_unobserved(self):
        no_rating = entry(title="Acme", domain="example.com", votes=100)
        no_votes = entry(title="Acme", domain="example.com", rating=4.8)
        for you in (no_rating, no_votes):
            out = evaluate(self.pack(you, [_rated(4.5, 100)]))["J-07"]
            assert out["status"] == "inconclusive"
            assert "your rating/votes unobserved" in out["note"]

    def test_inconclusive_competitor_ratings_unobserved(self):
        alone = evaluate(self.pack(YOU_FULL, []))["J-07"]
        unrated = evaluate(self.pack(YOU_FULL, [entry(title="Rival")]))["J-07"]
        for out in (alone, unrated):
            assert out["status"] == "inconclusive"
            assert "competitor ratings unobserved" in out["note"]

    def test_inconclusive_you_never_observed(self):
        out = evaluate(pack_of(sighting(you=None, others=[_rated(4.5, 100)])))["J-07"]
        assert out["status"] == "inconclusive"

    def test_median_determinism_repeat_evaluations_identical(self):
        pack = self.pack(YOU_FULL, [_rated(4.9, 500), _rated(4.1, 80), _rated(4.7, 120)])
        first = evaluate(pack)
        for _ in range(3):
            assert evaluate(pack) == first

    def test_every_check_evaluated_with_deterministic_source(self):
        out = evaluate(pack_of())
        assert sorted(out) == sorted(LP_IDS)
        assert all(v["source"] == "deterministic" for v in out.values())


# ---------------------------------------------------------------------------
# registry golden — v1.4.0, 106 checks, the new fix_type accepted
# ---------------------------------------------------------------------------


class TestRegistryGolden:
    def test_v140_with_106_checks_and_j_family(self):
        reg = load_registry()  # loader validates; raising here fails the test
        assert reg.version == "v1.4.0"
        assert len(reg.checks) == 106
        assert sum(1 for c in reg.checks.values() if c["category"] == "J") == 7
        for cid in LP_IDS:
            check = reg.checks[cid]
            assert check["check_version"] == 1
            assert check["method"] == "deterministic"
            assert check["badge"] == "measured"
        assert "local_listing" in VALID_FIX_TYPES

    def test_contract_table_pins(self):
        reg = load_registry()
        assert (reg.checks["J-05"]["weight"], reg.checks["J-05"]["severity"]) == (2, "medium")
        assert reg.checks["J-05"]["fix_type"] == "local_listing"
        assert (reg.checks["J-06"]["weight"], reg.checks["J-06"]["severity"]) == (2, "medium")
        assert reg.checks["J-06"]["fix_type"] == "local_listing"
        assert (reg.checks["J-07"]["weight"], reg.checks["J-07"]["severity"]) == (1, "low")
        assert reg.checks["J-07"]["fix_type"] == "offpage_entity"

    def test_diagnostic_only_fix_templates(self):
        # the do-not-build law: templates point at the client's own legitimate
        # in-person process and never instruct generating/automating reviews
        reg = load_registry()
        t = reg.checks["J-07"]["fix_template"]
        assert "in-person" in t and "Never generate" in t


# ---------------------------------------------------------------------------
# pipeline wiring (pure): draft-NA membership + classifier exclusion proof
# ---------------------------------------------------------------------------


@dataclass
class FakeResult:
    text: str
    parsed: object = None
    usage: dict = field(default_factory=dict)
    cost_cents: float = 0.0


class FakeLlm:
    """Answers 'pass' for every check in the prompt; records every prompt."""

    model = "fake-model"

    def __init__(self):
        self.prompts: list[str] = []

    def complete(self, *, system, user, budget=None, **kw):
        self.prompts.append(user)
        block = user.split("EVIDENCE BUNDLE", 1)[0]
        ids = list(dict.fromkeys(re.findall(r'"check_id":"([A-J]-\d+)"', block)))
        text = json.dumps([{"check_id": i, "status": "pass", "note": "ok"} for i in ids])
        return FakeResult(text=text, parsed=json.loads(text))


class TestPipelineWiring:
    def test_family_joins_draft_na_check_ids(self):
        assert set(LP_IDS) <= pipeline.DRAFT_NA_CHECK_IDS
        assert set(LP_IDS) <= pipeline.draft_na_check_ids(load_registry())

    def test_overrides_never_reach_classifier(self):
        reg = load_registry()
        llm = FakeLlm()
        overrides = {
            cid: {"status": "na", "note": pipeline.LOCAL_PRESENCE_NA_NOTE,
                  "source": "deterministic"}
            for cid in LP_IDS
        }
        overrides.update(pipeline.comparative_na_overrides(reg, {}))
        status_map, _, _ = pipeline.classify_checks(
            llm, reg, {"page": {}}, pipeline.CallBudget(100.0), overrides=overrides
        )
        assert llm.prompts, "classifier should still run for the other categories"
        for prompt in llm.prompts:
            for cid in LP_IDS:
                assert cid not in prompt
        assert "J-01" in "".join(llm.prompts)  # the rest of J still classifies
        for cid in LP_IDS:
            assert status_map[cid]["source"] == "deterministic"

    def test_mini_registry_without_family_returns_no_overrides(self):
        # no J-05..07 in the registry -> {} and ZERO DB reads (conn=None proves it)
        from gm.audit.registry import Registry

        reg = Registry(version="vtest", checks={})
        assert pipeline.local_presence_audit_overrides(None, "sid", reg, "https://x.com") == {}


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated():
    db.run_migrations()
    # Migration 011 owns the queue_items kind constraint (adds 'local_presence',
    # carries 010's 'competitor_candidate'). Rely on it — assert rather than
    # patch, so a regression in 011 fails loudly here instead of being masked.
    with db.connect(autocommit=True) as c:
        row = c.execute(
            "select pg_get_constraintdef(oid) as def from pg_constraint"
            " where conrelid = 'queue_items'::regclass"
            " and conname = 'queue_items_kind_check'"
        ).fetchone()
        assert row is not None and "local_presence" in row["def"], (
            "migration 011 must extend queue_items_kind_check with 'local_presence'"
        )


@pytest.fixture()
def conn(_migrated):
    with db.connect(autocommit=True) as c:
        yield c


@pytest.fixture()
def site(conn):
    org_id = conn.execute("insert into orgs (name) values ('lp-t') returning id"
                          ).fetchone()["id"]
    site_id = conn.execute(
        "insert into sites (org_id, domain_norm, brand_terms) values (%s, %s, %s) returning id",
        (org_id, f"lp-{uuid.uuid4().hex[:10]}.example", ["Acme Clinic"]),
    ).fetchone()["id"]
    return {"org_id": org_id, "site_id": site_id}


def _track(conn, site, query, active=True):
    conn.execute(
        "insert into tracked_queries (org_id, site_id, query_norm, active)"
        " values (%s, %s, %s, %s)",
        (site["org_id"], site["site_id"], serp.query_norm(query), active),
    )


def _snapshot(conn, site, query, features, *, age_days=0):
    conn.execute(
        "insert into serp_snapshots (org_id, site_id, query_norm, results, features,"
        " fetched_at) values (%s, %s, %s, %s, %s, now() - make_interval(days => %s))",
        (site["org_id"], site["site_id"], serp.query_norm(query), Jsonb([]),
         Jsonb(features), age_days),
    )


def _pack_feature(*entries):
    return [{"type": "local_pack", "entries": list(entries)}]


# ---------------------------------------------------------------------------
# DB: collect_local_pack
# ---------------------------------------------------------------------------


@needs_db
class TestCollectLocalPack:
    def test_newest_in_window_snapshot_per_query_wins(self, conn, site):
        _track(conn, site, "kw")
        old_you = entry(title="Acme Clinic", phone="+971 4 000")
        _snapshot(conn, site, "kw", _pack_feature(old_you), age_days=10)
        _snapshot(conn, site, "kw", _pack_feature(YOU_FULL), age_days=1)
        pack = local_presence.collect_local_pack(conn, site["site_id"])
        assert pack["packs_seen"] == 1
        assert pack["sightings"][0]["you"]["phone"] == YOU_FULL["phone"]

    def test_out_of_window_snapshots_are_invisible(self, conn, site):
        _track(conn, site, "kw")
        _snapshot(conn, site, "kw", _pack_feature(YOU_FULL), age_days=40)
        pack = local_presence.collect_local_pack(conn, site["site_id"])
        assert pack["packs_seen"] == 0 and pack["you_seen"] == 0
        assert "no local-pack sighting" in pack["note"]

    def test_inactive_queries_do_not_contribute(self, conn, site):
        _track(conn, site, "kw", active=False)
        _snapshot(conn, site, "kw", _pack_feature(YOU_FULL), age_days=1)
        assert local_presence.collect_local_pack(conn, site["site_id"])["packs_seen"] == 0

    def test_legacy_pack_without_entries_counts_unobserved(self, conn, site):
        _track(conn, site, "kw")
        _snapshot(conn, site, "kw", [{"type": "local_pack"}], age_days=1)  # pre-D3 shape
        pack = local_presence.collect_local_pack(conn, site["site_id"])
        assert pack["packs_seen"] == 0
        assert pack["packs_unobserved"] == 1
        assert "entries unobserved" in pack["note"]

    def test_serp_without_local_pack_contributes_nothing(self, conn, site):
        _track(conn, site, "kw")
        _snapshot(conn, site, "kw", [{"type": "people_also_ask", "questions": []}], age_days=1)
        pack = local_presence.collect_local_pack(conn, site["site_id"])
        assert pack["packs_seen"] == 0 and pack["packs_unobserved"] == 0
        assert "no local-pack sighting" in pack["note"]

    def test_you_match_brand_term_and_subdomain(self, conn, site):
        domain = conn.execute(
            "select domain_norm from sites where id = %s", (site["site_id"],)
        ).fetchone()["domain_norm"]
        _track(conn, site, "brand kw")
        _track(conn, site, "domain kw")
        _track(conn, site, "foreign kw")
        by_brand = entry(title="ACME clinic", domain="some-directory.com")
        by_domain = entry(title="Some Listing Name", url=f"https://maps.{domain}/place")
        foreign = entry(title="Rival Spa", domain="rival.com")
        _snapshot(conn, site, "brand kw", _pack_feature(by_brand, foreign), age_days=1)
        _snapshot(conn, site, "domain kw", _pack_feature(foreign, by_domain), age_days=1)
        _snapshot(conn, site, "foreign kw", _pack_feature(foreign), age_days=1)
        pack = local_presence.collect_local_pack(conn, site["site_id"])
        assert pack["packs_seen"] == 3
        assert pack["you_seen"] == 2
        by_query = {s["query_norm"]: s["you"] for s in pack["sightings"]}
        assert by_query["brand kw"]["title"] == "ACME clinic"
        assert by_query["domain kw"]["title"] == "Some Listing Name"
        assert by_query["foreign kw"] is None


# ---------------------------------------------------------------------------
# DB: local_presence_overrides + queue detector (upsert/snooze discipline)
# ---------------------------------------------------------------------------


@needs_db
class TestOverridesAndQueue:
    def test_overrides_restricted_to_registry_ids(self, conn, site):
        reg = load_registry()
        out = local_presence.local_presence_overrides(conn, site["site_id"], reg)
        assert sorted(out) == sorted(LP_IDS)
        assert all(v["status"] == "inconclusive" for v in out.values())  # honest empty state

    def _rows(self, conn, site):
        return conn.execute(
            "select * from queue_items where site_id = %s and kind = 'local_presence'"
            " order by target_hash",
            (site["site_id"],),
        ).fetchall()

    def _plant_fail(self, conn, site):
        """packs observed for 2 queries, you in none -> J-05 fail; J-06/J-07 inconclusive."""
        rival = entry(title="Rival Spa", domain="rival.com", rating=4.5, votes=100)
        for q in ("kw one", "kw two"):
            _track(conn, site, q)
            _snapshot(conn, site, q, _pack_feature(rival), age_days=1)

    def test_upsert_on_fail_with_contract_at_stake(self, conn, site):
        self._plant_fail(conn, site)
        touched = local_presence.detect_local_presence(conn, site["site_id"])
        assert touched == 1
        (row,) = self._rows(conn, site)
        assert row["target"] == {"check_id": "J-05"}
        assert row["at_stake"]["basis"] == "serp_local_pack"
        assert row["at_stake"]["queries_with_pack"] == 2
        assert "you appear in none" in row["at_stake"]["issue"]
        assert row["status"] == "open"

    def test_rerun_is_idempotent_one_row(self, conn, site):
        self._plant_fail(conn, site)
        assert local_presence.detect_local_presence(conn, site["site_id"]) == 1
        assert local_presence.detect_local_presence(conn, site["site_id"]) == 1
        assert len(self._rows(conn, site)) == 1

    def test_snooze_elapsed_reopens_future_stays_dismissed(self, conn, site):
        self._plant_fail(conn, site)
        local_presence.detect_local_presence(conn, site["site_id"])
        conn.execute(
            "update queue_items set status='dismissed', snooze_until=now() - interval '1 day'"
            " where site_id = %s and kind = 'local_presence'",
            (site["site_id"],),
        )
        local_presence.detect_local_presence(conn, site["site_id"])
        (row,) = self._rows(conn, site)
        assert row["status"] == "open" and row["snooze_until"] is None

        conn.execute(
            "update queue_items set status='dismissed', snooze_until=now() + interval '7 days'"
            " where id = %s", (row["id"],),
        )
        local_presence.detect_local_presence(conn, site["site_id"])
        (row,) = self._rows(conn, site)
        assert row["status"] == "dismissed"  # future snooze respected

    def test_actioned_rows_never_touched(self, conn, site):
        self._plant_fail(conn, site)
        local_presence.detect_local_presence(conn, site["site_id"])
        conn.execute(
            "update queue_items set status='actioned', at_stake='{}'::jsonb"
            " where site_id = %s and kind = 'local_presence'",
            (site["site_id"],),
        )
        local_presence.detect_local_presence(conn, site["site_id"])
        (row,) = self._rows(conn, site)
        assert row["status"] == "actioned" and row["at_stake"] == {}

    def test_pass_and_inconclusive_never_enqueue(self, conn, site):
        # complete you-entry (on the site's own domain) across two queries,
        # above pack medians -> J-05/J-06/J-07 all pass, nothing enqueued
        domain = conn.execute(
            "select domain_norm from sites where id = %s", (site["site_id"],)
        ).fetchone()["domain_norm"]
        you = entry(title="Acme Clinic", domain=domain, url=f"https://{domain}/",
                    phone="+971 4 123 4567", rating=4.8, votes=200)
        rivals = [_rated(4.0, 50), _rated(4.2, 80)]
        for q in ("kw one", "kw two"):
            _track(conn, site, q)
            _snapshot(conn, site, q, _pack_feature(you, *rivals), age_days=1)
        assert local_presence.detect_local_presence(conn, site["site_id"]) == 0
        assert self._rows(conn, site) == []

    def test_detector_visible_through_compute_queue(self, conn, site):
        from gm.intel import detectors

        self._plant_fail(conn, site)
        result = detectors.compute_queue(conn, str(site["site_id"]))
        assert result["counts"]["local_presence"] == 1
        assert "local_presence" not in result["skipped"]


# ---------------------------------------------------------------------------
# DB: audit pipeline end-to-end — deterministic statuses, classifier exclusion,
# reference-NA, draft-NA
# ---------------------------------------------------------------------------

PAGE_HTML = """<html><head><title>Acme Clinic</title></head>
<body><h1>Acme outpatient clinic</h1>
<p>Acme Clinic provides outpatient care, aesthetic treatments, and follow-up
programs for patients across the city. Our team documents visits, tracks
outcomes, and keeps every treatment plan current so patients always know the
next step of their care journey with us.</p></body></html>"""

NOT_FOUND_HTML = (
    "<html><head><title>404</title></head>"
    "<body><h1>Page not found</h1><p>This page does not exist.</p></body></html>"
)


def fake_fetcher_factory(user_agent: str):
    from gm.audit.bev import NOT_FOUND_PATH

    def fetch(url: str) -> FetchResult:
        low = url.lower()
        status, text = 200, PAGE_HTML
        if low.endswith("/robots.txt"):
            text = "User-agent: *\nAllow: /\n"
        elif NOT_FOUND_PATH.lower() in low or not low.rstrip("/").endswith("/page"):
            status, text = 404, NOT_FOUND_HTML
        return FetchResult(
            url=url, final_url=url, status_code=status,
            headers={"content-type": "text/html"}, text=text,
            elapsed_ms=1, redirect_chain=[url],
        )

    return fetch


@pytest.fixture
def public_dns(monkeypatch):
    def resolve(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(safety, "_getaddrinfo", resolve)


@needs_db
class TestAuditSurfacing:
    def _audit(self, conn, site, url):
        llm = FakeLlm()
        with db.connect() as tx:
            db.set_org(tx, site["org_id"])
            audit_id = pipeline.run_page_audit(
                tx, org_id=str(site["org_id"]), site_id=str(site["site_id"]), url=url,
                llm=llm, registry=load_registry(), fetcher_factory=fake_fetcher_factory,
            )
            tx.commit()
        audit = conn.execute("select * from audits where id=%s", (audit_id,)).fetchone()
        findings = {
            f["check_id"]: f
            for f in conn.execute(
                "select * from audit_findings where audit_id=%s", (audit_id,)
            ).fetchall()
        }
        return audit, findings, llm

    def _client_url(self, conn, site):
        domain = conn.execute(
            "select domain_norm from sites where id=%s", (site["site_id"],)
        ).fetchone()["domain_norm"]
        return f"https://{domain}/page"

    def test_client_site_audit_gets_deterministic_statuses_never_classifier(
        self, conn, site, public_dns
    ):
        _track(conn, site, "kw")
        rivals = [_rated(4.9, 500), _rated(4.9, 400)]
        _snapshot(conn, site, "kw", _pack_feature(YOU_FULL, *rivals), age_days=1)

        audit, findings, llm = self._audit(conn, site, self._client_url(conn, site))
        assert audit["status"] == "done"
        # graded deterministically from the planted pack: complete entry ->
        # J-05 pass; one sighting -> J-06 inconclusive; votes 200 < half the
        # 450 pack median -> J-07 fail
        assert findings["J-05"]["status"] == "pass"
        assert findings["J-06"]["status"] == "inconclusive"
        assert findings["J-07"]["status"] == "fail"
        for cid in LP_IDS:
            assert findings[cid]["evidence"]["source"] == "deterministic"
        # the classifier ran (real registry, 10 categories) but NEVER saw the family
        assert llm.prompts
        for prompt in llm.prompts:
            for cid in LP_IDS:
                assert cid not in prompt
        assert findings["J-01"]["evidence"]["source"] == "llm"

    def test_foreign_page_audit_is_na_client_site_diagnostic(self, conn, site, public_dns):
        # compare.py runs competitor pages under the CLIENT's site_id, then tags
        # them competitor_reference — the local-presence family must be 'na'.
        audit, findings, llm = self._audit(conn, site, "https://rival-spa.com/page")
        assert audit["status"] == "done"
        for cid in LP_IDS:
            assert findings[cid]["status"] == "na"
            assert findings[cid]["evidence"]["note"] == "client-site diagnostic"
            assert findings[cid]["evidence"]["source"] == "deterministic"
        for prompt in llm.prompts:
            for cid in LP_IDS:
                assert cid not in prompt

    def test_draft_audit_is_na_pre_publish(self, conn, site):
        llm = FakeLlm()
        with db.connect() as tx:
            db.set_org(tx, site["org_id"])
            audit_id = pipeline.run_draft_audit(
                tx, org_id=str(site["org_id"]), site_id=str(site["site_id"]),
                draft_html=PAGE_HTML, url_hint="https://example.com/draft",
                llm=llm, registry=load_registry(),
            )
            tx.commit()
        findings = {
            f["check_id"]: f
            for f in conn.execute(
                "select * from audit_findings where audit_id=%s", (audit_id,)
            ).fetchall()
        }
        for cid in LP_IDS:
            assert findings[cid]["status"] == "na"
            assert findings[cid]["evidence"]["note"] == "not applicable pre-publish"
        for prompt in llm.prompts:
            for cid in LP_IDS:
                assert cid not in prompt
