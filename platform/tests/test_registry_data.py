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


def test_registry_root_env_override_and_cwd_fallback(tmp_path, monkeypatch):
    # GM_REGISTRY_DIR wins over everything (the prod-container escape hatch —
    # a pip-installed package resolves __file__ into site-packages, where the
    # parents[4] walk lands on nonsense like /usr/local/lib/registry).
    from gm.audit import citations as citations_mod
    from gm.audit import registry as registry_mod

    real_root = registry_mod._default_root()
    override = tmp_path / "reg"
    override.mkdir()
    monkeypatch.setenv("GM_REGISTRY_DIR", str(override))
    assert registry_mod._default_root() == override
    assert citations_mod._default_root() == override / "brain"

    # Unset -> the editable-install path still resolves to the real repo copy.
    monkeypatch.delenv("GM_REGISTRY_DIR")
    assert registry_mod._default_root() == real_root
    assert (real_root / "manifest.json").is_file()
