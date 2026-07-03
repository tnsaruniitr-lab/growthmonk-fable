"""Pure-logic tests for gm.audit.registry, gm.audit.scoring, gm.audit.delta.

No DB, no network. The registry used here is a small inline sample written to
tmp_path — the real registry/ files are extracted concurrently and must NOT be
depended on.
"""

import json

import pytest

from gm.audit.delta import audit_delta
from gm.audit.registry import Registry, RegistryError, load_registry
from gm.audit.scoring import (
    BAP_GROUPS,
    PCR_WEIGHTS,
    VALID_GRADES,
    compute_demand_capture,
    grade_for,
    recompute_scores,
    validate_findings,
)

# ---------------------------------------------------------------------------
# sample registry helpers
# ---------------------------------------------------------------------------


def make_check(check_id: str, category: str, weight: float = 1,
               badge: str = "static_rule", fix_type: str = "page_html",
               version: int = 1) -> dict:
    return {
        "check_id": check_id,
        "check_version": version,
        "category": category,
        "category_name": f"Category {category}",
        "name": f"Check {check_id}",
        "description": "sample",
        "applies_to": ["all"],
        "method": "deterministic",
        "badge": badge,
        "fix_type": fix_type,
        "criteria": {"pass": "p", "warn": "w", "fail": "f"},
        "weight": weight,
        "severity": "medium",
        "fix_template": "",
        "sources": [],
    }


def write_registry(tmp_path, checks_by_letter: dict[str, list[dict]],
                   version: str = "v-test-1") -> Registry:
    root = tmp_path / "registry"
    (root / "checks").mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"version": version}))
    for letter, checks in checks_by_letter.items():
        (root / "checks" / f"{letter.lower()}.json").write_text(json.dumps(checks))
    return load_registry(root)


@pytest.fixture
def registry(tmp_path) -> Registry:
    return write_registry(tmp_path, {
        "A": [make_check("A-01", "A"), make_check("A-02", "A")],
        "D": [make_check("D-01", "D"), make_check("D-02", "D")],
        "I": [make_check(f"I-0{n}", "I") for n in (1, 2, 3, 5)],
        "J": [make_check("J-01", "J")],
    })


def finding(check_id: str, status: str, version: int = 1, badge: str = "static_rule") -> dict:
    return {"check_id": check_id, "check_version": version, "status": status, "badge": badge}


BASE = [finding("A-01", "pass"), finding("A-02", "fail"),
        finding("D-01", "pass"), finding("D-02", "warn")]
# A = 50.0, D = 75.0 -> PCR = (50*0.16 + 75*0.16) / 0.32 = 62.5 -> grade C


# ---------------------------------------------------------------------------
# registry loader
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_load_ok(self, registry):
        assert registry.version == "v-test-1"
        assert len(registry.checks) == 9
        assert registry.category_of("A-01") == "A"
        assert registry.category_of("Q-99") is None
        assert registry.weight_of("A-01") == 1.0
        assert registry.weight_of("nonexistent") == 1.0

    def test_missing_manifest_raises(self, tmp_path):
        (tmp_path / "registry" / "checks").mkdir(parents=True)
        with pytest.raises(RegistryError, match="manifest"):
            load_registry(tmp_path / "registry")

    def test_duplicate_check_id_raises(self, tmp_path):
        with pytest.raises(RegistryError, match="duplicate"):
            write_registry(tmp_path, {"A": [make_check("A-01", "A"), make_check("A-01", "A")]})

    @pytest.mark.parametrize("field", ["badge", "fix_type", "weight", "check_version"])
    def test_missing_required_field_raises(self, tmp_path, field):
        broken = make_check("A-01", "A")
        del broken[field]
        with pytest.raises(RegistryError, match="A-01"):
            write_registry(tmp_path, {"A": [broken]})

    def test_bad_badge_enum_raises(self, tmp_path):
        with pytest.raises(RegistryError, match="badge"):
            write_registry(tmp_path, {"A": [make_check("A-01", "A", badge="vibes")]})

    def test_category_letter_must_match_filename(self, tmp_path):
        # a B-category check inside a.json
        with pytest.raises(RegistryError, match="category"):
            write_registry(tmp_path, {"A": [make_check("B-01", "B")]})

    def test_check_id_letter_must_match_filename(self, tmp_path):
        with pytest.raises(RegistryError, match="B-01"):
            write_registry(tmp_path, {"A": [make_check("B-01", "A")]})

    def test_non_array_file_raises(self, tmp_path):
        root = tmp_path / "registry"
        (root / "checks").mkdir(parents=True)
        (root / "manifest.json").write_text(json.dumps({"version": "v"}))
        (root / "checks" / "a.json").write_text(json.dumps({"not": "a list"}))
        with pytest.raises(RegistryError, match="array"):
            load_registry(root)


# ---------------------------------------------------------------------------
# scoring — weights, sections, PCR, grades
# ---------------------------------------------------------------------------


class TestWeightTables:
    def test_pcr_weights_sum_to_one(self):
        assert sum(PCR_WEIGHTS.values()) == pytest.approx(1.0, abs=1e-9)

    def test_pcr_excludes_geo_section(self):
        assert "I_geo" not in PCR_WEIGHTS

    def test_bap_weights_sum_to_one(self):
        assert sum(w for _, w in BAP_GROUPS.values()) == pytest.approx(1.0, abs=1e-9)

    def test_grade_enum_has_nine_grades_plus_inconclusive(self):
        assert len(VALID_GRADES) == 10
        assert "INCONCLUSIVE" in VALID_GRADES


class TestGradeTable:
    @pytest.mark.parametrize("score,grade", [
        (95.0, "A+"), (94.9, "A"), (85.0, "A"), (84.9, "B+"),
        (80.0, "B+"), (79.9, "B"), (75.0, "B"), (74.9, "C+"),
        (68.0, "C+"), (67.9, "C"), (60.0, "C"), (59.9, "D+"),
        (53.0, "D+"), (52.9, "D"), (45.0, "D"), (44.9, "F"), (0.0, "F"),
    ])
    def test_boundaries(self, score, grade):
        assert grade_for(score) == grade

    def test_clamps_out_of_range(self):
        assert grade_for(150.0) == "A+"
        assert grade_for(-5.0) == "F"

    def test_none_is_inconclusive(self):
        assert grade_for(None) == "INCONCLUSIVE"


class TestRecomputeScores:
    def test_sections_pcr_grade(self, registry):
        res = recompute_scores(BASE, registry, "ok")
        assert res["section_scores"]["A_technical"] == 50.0
        assert res["section_scores"]["D_schema"] == 75.0
        assert res["section_scores"]["B_performance"] is None
        assert res["page_citation_readiness"] == 62.5
        assert res["overall_score"] == 62.5
        assert res["overall_grade"] == "C"
        assert res["grade_basis"] == "page_citation_readiness"
        assert res["inconclusive"] is False
        assert res["computed_by"] == "runtime-deterministic"
        assert res["registry_version"] == "v-test-1"

    def test_deterministic_byte_stable(self, registry):
        a = recompute_scores([dict(f) for f in BASE], registry, "ok")
        b = recompute_scores([dict(f) for f in BASE], registry, "ok")
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def test_na_renormalization(self, registry):
        # D entirely na -> excluded; PCR rides on A alone.
        rows = [finding("A-01", "pass"), finding("A-02", "fail"),
                finding("D-01", "na"), finding("D-02", "inconclusive")]
        res = recompute_scores(rows, registry, "ok")
        assert res["section_scores"]["D_schema"] is None
        assert res["section_counts"]["D_schema"]["na"] == 1
        assert res["section_counts"]["D_schema"]["inconclusive"] == 1
        assert res["page_citation_readiness"] == 50.0

    def test_per_check_registry_weights(self, tmp_path):
        reg = write_registry(tmp_path, {
            "A": [make_check("A-01", "A", weight=3), make_check("A-02", "A", weight=1)],
        })
        res = recompute_scores([finding("A-01", "pass"), finding("A-02", "fail")], reg, "ok")
        assert res["section_scores"]["A_technical"] == 75.0

    def test_forged_status_neutralized_to_na(self, registry):
        rows = [finding("A-01", "pass"), finding("A-02", '99"><script>alert(1)</script>')]
        res = recompute_scores(rows, registry, "ok")
        # forged status scores as na -> A section is 1/1 pass
        assert res["section_scores"]["A_technical"] == 100.0
        assert res["section_counts"]["A_technical"]["na"] == 1
        assert any("out of enum" in n for n in res["validation_notes"])
        assert res["overall_grade"] in VALID_GRADES

    def test_unknown_check_id_dropped_with_note(self, registry):
        rows = BASE + [finding("A-99", "fail"), finding("Z-01", "fail")]
        res = recompute_scores(rows, registry, "ok")
        assert res["page_citation_readiness"] == 62.5  # unknowns did not count
        dropped = [n for n in res["validation_notes"] if "unknown check_id" in n]
        assert len(dropped) == 2

    def test_no_gradeable_checks_is_inconclusive(self, registry):
        res = recompute_scores([finding("A-01", "na")], registry, "ok")
        assert res["inconclusive"] is True
        assert res["overall_grade"] == "INCONCLUSIVE"
        assert res["page_citation_readiness"] is None


class TestTransportGate:
    @pytest.mark.parametrize("gate", [
        "transport_inconclusive", "robots_blocked", "bot_blocked",
        "unresolved_redirect", "http_error", "fetch_failed",
    ])
    def test_gate_refuses_to_grade(self, registry, gate):
        res = recompute_scores(BASE, registry, gate)
        assert res["inconclusive"] is True
        assert res["overall_grade"] == "INCONCLUSIVE"
        assert res["overall_score"] is None
        assert res["page_citation_readiness"] is None
        assert res["demand_capture"] is None
        assert gate in res["inconclusive_reason"]

    def test_ok_gate_grades(self, registry):
        assert recompute_scores(BASE, registry, "ok")["overall_grade"] == "C"


class TestBapSeparation:
    I_FAIL = [finding(f"I-0{n}", "fail") for n in (1, 2, 3, 5)]
    I_PASS = [finding(f"I-0{n}", "pass") for n in (1, 2, 3, 5)]

    def test_bap_never_folds_into_grade(self, registry):
        worst = recompute_scores(BASE + self.I_FAIL, registry, "ok")
        best = recompute_scores(BASE + self.I_PASS, registry, "ok")
        assert worst["brand_ai_presence"] == 0.0
        assert best["brand_ai_presence"] == 100.0
        # identical PCR and letter grade regardless of GEO outcomes
        assert worst["page_citation_readiness"] == best["page_citation_readiness"] == 62.5
        assert worst["overall_grade"] == best["overall_grade"] == "C"

    def test_bap_group_weighting(self, registry):
        rows = BASE + [finding("I-01", "pass"), finding("I-02", "fail"),
                       finding("I-03", "warn"), finding("I-05", "pass")]
        res = recompute_scores(rows, registry, "ok")
        # presence 50*0.40 + accuracy 50*0.35 + favorability 100*0.25 = 62.5
        assert res["brand_ai_presence"] == 62.5
        assert res["brand_ai_presence_confidence"] == "medium"

    def test_bap_none_without_geo_checks(self, registry):
        res = recompute_scores(BASE, registry, "ok")
        assert res["brand_ai_presence"] is None
        assert res["brand_ai_presence_confidence"] == "none"


class TestDemandCapture:
    def test_headline_blend(self, registry):
        res = recompute_scores(BASE + TestBapSeparation.I_FAIL, registry, "ok")
        # PCR 62.5, BAP 0.0 -> 62.5*0.8 + 0*0.2 = 50.0
        assert res["demand_capture"] == 50.0

    def test_falls_back_to_pcr_without_bap(self, registry):
        res = recompute_scores(BASE, registry, "ok")
        assert res["demand_capture"] == 62.5

    def test_none_without_pcr(self):
        assert compute_demand_capture(None, 80.0) is None

    def test_forged_inputs_neutralized(self):
        assert compute_demand_capture('62.5"><img onerror=x>', None) == 62.5
        assert compute_demand_capture(True, None) is None
        assert compute_demand_capture(float("nan"), None) is None
        assert compute_demand_capture(1e9, 1e9) == 100.0  # clamped to [0,100]


class TestValidateFindings:
    def test_badge_overridden_from_registry(self, registry):
        rows = validate_findings([finding("A-01", "pass", badge="hard_evidence")], registry)
        assert rows[0]["badge"] == "static_rule"

    def test_check_version_repaired(self, registry):
        rows = validate_findings([{"check_id": "A-01", "check_version": "not-a-number",
                                   "status": "pass", "badge": "static_rule"}], registry)
        assert rows[0]["check_version"] == 1

    def test_unknown_and_malformed_dropped(self, registry):
        rows = validate_findings(
            [finding("A-01", "pass"), finding("A-99", "fail"), "junk", {}],  # type: ignore
            registry,
        )
        assert [r["check_id"] for r in rows] == ["A-01"]


# ---------------------------------------------------------------------------
# delta — transitions + ADR-13 comparability
# ---------------------------------------------------------------------------


class TestDelta:
    def test_fable_reference_case(self):
        before = [finding("A-01", "fail"), finding("A-02", "pass"), finding("D-01", "warn")]
        after = [finding("A-01", "pass"), finding("A-02", "fail"),
                 finding("D-01", "warn"), finding("G-01", "fail")]
        d = audit_delta(before, after,
                        before_scores={"overall_score": 62, "overall_grade": "C"},
                        after_scores={"overall_score": 81, "overall_grade": "B+"})
        assert d["resolved"] == ["A-01"]
        assert d["regressed"] == ["A-02"]
        assert d["new_issues"] == ["G-01"]
        assert d["persisting"] == ["D-01"]
        assert d["non_comparable"] == []
        assert d["score_delta"]["change"] == 19.0
        assert d["score_delta"]["grade_prior"] == "C"
        assert d["score_delta"]["grade_current"] == "B+"
        assert "1 resolved, 1 regressed, 1 new, 1 still open" in d["summary"]

    def test_version_mismatch_is_non_comparable(self):
        # would look "resolved" if versions were ignored — ADR-13 forbids that
        d = audit_delta([finding("A-01", "fail", version=1)],
                        [finding("A-01", "pass", version=2)])
        assert d["resolved"] == []
        assert d["regressed"] == []
        assert d["non_comparable"] == [
            {"check_id": "A-01", "before_version": 1, "after_version": 2}
        ]
        assert d["counts"]["non_comparable"] == 1
        assert "not comparable" in d["summary"]

    def test_version_missing_one_side_is_non_comparable(self):
        d = audit_delta([{"check_id": "A-01", "status": "fail"}],
                        [finding("A-01", "pass", version=1)])
        assert d["resolved"] == []
        assert d["non_comparable"][0]["before_version"] is None

    def test_matching_versions_compare_normally(self):
        d = audit_delta([finding("A-01", "fail", version=2)],
                        [finding("A-01", "pass", version=2)])
        assert d["resolved"] == ["A-01"]
        assert d["non_comparable"] == []

    def test_na_to_fail_is_new_not_regressed(self):
        d = audit_delta([finding("A-01", "na"), finding("A-02", "inconclusive")],
                        [finding("A-01", "fail"), finding("A-02", "warn")])
        assert sorted(d["new_issues"]) == ["A-01", "A-02"]
        assert d["regressed"] == []

    def test_regressed_only_from_pass(self):
        d = audit_delta([finding("A-01", "pass")], [finding("A-01", "warn")])
        assert d["regressed"] == ["A-01"]

    def test_absent_transitions(self):
        # gone-good counts as resolved; brand-new bad check is a new issue
        d = audit_delta([finding("A-01", "fail")], [finding("D-01", "fail")])
        assert d["resolved"] == ["A-01"]
        assert d["new_issues"] == ["D-01"]

    def test_no_scores_given(self):
        d = audit_delta([], [finding("A-01", "fail")])
        assert d["score_delta"] == {"prior": None, "current": None, "change": None,
                                    "grade_prior": None, "grade_current": None}
        assert "no prior score to compare" in d["summary"]

    def test_deterministic_output(self):
        before = [finding("D-01", "warn"), finding("A-01", "fail")]
        after = [finding("A-01", "pass"), finding("D-01", "fail", version=2)]
        a = audit_delta(list(before), list(after))
        b = audit_delta(list(reversed(before)), list(reversed(after)))
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
