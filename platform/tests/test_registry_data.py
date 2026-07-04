"""Pins the REAL registry data files: a bad edit to registry/checks/*.json fails CI."""

from gm.audit.registry import load_registry
from gm.audit.scoring import recompute_scores


def test_real_registry_loads_complete():
    r = load_registry()
    assert len(r.checks) == 106
    assert r.version == "v1.4.0"
    by_cat: dict[str, int] = {}
    for c in r.checks.values():
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
    assert by_cat == {
        "A": 12, "B": 11, "C": 13, "D": 13, "E": 13,
        "F": 12, "G": 9, "H": 8, "I": 8, "J": 7,  # J 4->7: D3 local-presence family
    }


def test_real_registry_source_quirks_preserved():
    r = load_registry()
    # v1.1 renumbering: C-13 never existed; C-14 replaced it.
    assert "C-14" in r.checks
    assert "C-13" not in r.checks
    assert all(c["check_version"] == 1 for c in r.checks.values())


def test_scoring_runs_against_real_registry():
    r = load_registry()
    findings = [
        {"check_id": cid, "check_version": 1, "status": "pass", "badge": c["badge"]}
        for cid, c in r.checks.items()
    ]
    scores = recompute_scores(findings, r, "ok")
    assert scores["overall_grade"] == "A+"  # all-pass grades at the top of the table
    assert scores["page_citation_readiness"] == 100.0
    assert scores["computed_by"] == "runtime-deterministic"
    assert scores["inconclusive"] is False
