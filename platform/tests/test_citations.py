"""Citations layer: unit tests on a synthetic brain + integration seam against
the REAL copied snapshots in registry/brain/ (no DB, no network)."""

import json
import time

import pytest

from gm.audit.citations import (
    Brain,
    BrainError,
    attach_citations,
    clear_brain_cache,
    load_brain,
    normalize_check_id,
    rank_citations,
)
from gm.audit.registry import load_registry

# ---------------------------------------------------------------------------
# Synthetic brain (pure-unit tests)
# ---------------------------------------------------------------------------


def _mini_brain() -> Brain:
    rules = {
        10: {"id": 10, "name": "Tier-3 high conf", "source_org": "Moz",
             "source_url": "https://moz.com/x", "confidence_score": "0.99"},
        11: {"id": 11, "name": "Tier-1 low conf", "source_org": "Google",
             "source_url": "https://developers.google.com/x", "confidence_score": "0.70"},
        12: {"id": 12, "name": "Tier-1 high conf", "source_org": "Schema.org",
             "source_url": "https://schema.org/x", "confidence_score": "0.95"},
        13: {"id": 13, "name": "No url — must be dropped", "source_org": "Google",
             "confidence_score": "0.99"},
        14: {"id": 14, "name": "Tier-1 tie lower id", "source_org": "Google",
             "source_url": "https://developers.google.com/y", "confidence_score": "0.70"},
    }
    aps = {
        50: {"id": 50, "title": "Risky pattern", "source_org": "Ahrefs",
             "source_url": "https://ahrefs.com/x", "risk_level": "high"},
    }
    return Brain(
        rules_by_id=rules,
        aps_by_id=aps,
        mappings={"A1_https_enforcement": {"rules": [10, 11, 12, 13, 14],
                                           "anti_patterns": [50]}},
    )


def test_normalize_check_id_both_formats():
    assert normalize_check_id("A-01") == ("A", 1)
    assert normalize_check_id("A1_https_enforcement") == ("A", 1)
    assert normalize_check_id("d-13") == ("D", 13)
    assert normalize_check_id("J4_anything") == ("J", 4)
    assert normalize_check_id("misc_brand_without_schema") is None
    assert normalize_check_id("") is None


def test_rank_orders_tier_then_confidence_then_id():
    brain = _mini_brain()
    cits = rank_citations("A-01", brain=brain, limit=10)
    # Tier 1 first (12 conf .95, then 11/14 conf .70 by id), tier 2 AP, tier 3.
    assert [c["id"] for c in cits] == [12, 11, 14, 50, 10]
    assert 13 not in {c["id"] for c in cits}  # no source_url -> dropped
    assert cits[3]["kind"] == "anti_pattern"
    assert cits[3]["confidence"] == pytest.approx(0.95)  # high risk translated
    assert all(
        set(c) == {"id", "kind", "title", "source_org", "source_url", "tier", "confidence"}
        for c in cits
    )


def test_rank_limit_and_padded_id_resolves_unpadded_mapping():
    brain = _mini_brain()
    assert len(rank_citations("A-01", brain=brain)) == 3  # default limit
    # Unpadded lookup hits the same mapping.
    assert rank_citations("A1", brain=brain) == rank_citations("A-01", brain=brain)


def test_rank_unmapped_returns_empty():
    brain = _mini_brain()
    assert rank_citations("B-07", brain=brain) == []
    assert rank_citations("misc_nothing", brain=brain) == []


def test_rank_mappings_override():
    brain = _mini_brain()
    override = {"B7_custom": {"rules": [12]}}
    cits = rank_citations("B-07", brain=brain, mappings=override)
    assert [c["id"] for c in cits] == [12]


def test_attach_citations_statuses_and_empty_lists():
    brain = _mini_brain()
    findings = [
        {"check_id": "A-01", "status": "fail"},
        {"check_id": "A-01", "status": "warn"},
        {"check_id": "A-01", "status": "pass"},
        {"check_id": "B-07", "status": "fail"},  # no mapping
        {"check_id": "A-01", "status": "na"},
    ]
    out = attach_citations(findings, brain=brain)
    assert out is findings  # same list, mutated in place
    assert len(out[0]["citations"]) == 3
    assert len(out[1]["citations"]) == 3
    assert out[2]["citations"] == []
    assert out[3]["citations"] == []
    assert out[4]["citations"] == []


def test_load_brain_missing_root_raises(tmp_path):
    with pytest.raises(BrainError):
        load_brain(tmp_path)


# ---------------------------------------------------------------------------
# Integration seam: the REAL copied data in registry/brain/
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_brain() -> Brain:
    return load_brain()


def test_real_brain_snapshot_sizes(real_brain):
    stats = real_brain.stats()
    assert stats["rules"] == 4980
    assert stats["anti_patterns"] == 2843
    assert stats["principles"] == 3728
    assert stats["playbooks"] == 1213
    assert stats["mapped_checks"] >= 100


def test_real_registry_coverage_at_least_30(real_brain):
    registry = load_registry()
    assert len(registry.checks) == 106  # 103 extracted + D3 local-presence family (J-05..07)
    covered = [
        cid for cid in registry.checks
        if rank_citations(cid, brain=real_brain)
    ]
    assert len(covered) >= 30, f"only {len(covered)} of 106 checks resolve to a citation"


def test_real_every_citation_has_source_url_and_shape(real_brain):
    registry = load_registry()
    seen_any = False
    for cid in registry.checks:
        for cit in rank_citations(cid, brain=real_brain):
            seen_any = True
            assert cit["source_url"], f"{cid}: citation {cit['id']} lacks source_url"
            assert cit["kind"] in {"rule", "anti_pattern", "principle", "playbook"}
            assert isinstance(cit["tier"], int) and 1 <= cit["tier"] <= 5
            assert isinstance(cit["confidence"], float)
            assert cit["title"]
    assert seen_any


def test_real_ranking_deterministic_byte_equal(real_brain):
    registry = load_registry()
    first = json.dumps(
        {cid: rank_citations(cid, brain=real_brain) for cid in sorted(registry.checks)},
        sort_keys=True,
    )
    second = json.dumps(
        {cid: rank_citations(cid, brain=real_brain) for cid in sorted(registry.checks)},
        sort_keys=True,
    )
    assert first.encode() == second.encode()


def test_real_cold_load_under_2s():
    clear_brain_cache()
    try:
        start = time.perf_counter()
        brain = load_brain()
        elapsed = time.perf_counter() - start
        assert brain.rules_by_id
        assert elapsed < 2.0, f"cold brain load took {elapsed:.2f}s"
        # Warm hit is the cached object — the pipeline never re-parses per finding.
        assert load_brain() is brain
    finally:
        clear_brain_cache()


def test_real_tier1_sources_rank_first(real_brain):
    # A-01 maps to the HTTPS rule/AP pair; spot-check a known tier-1 ordering
    # elsewhere: any check whose candidates span tiers must list tier 1 first.
    registry = load_registry()
    for cid in registry.checks:
        cits = rank_citations(cid, brain=real_brain)
        tiers = [c["tier"] for c in cits]
        assert tiers == sorted(tiers), f"{cid}: tiers out of order {tiers}"
