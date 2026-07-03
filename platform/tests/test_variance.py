"""Pure-logic tests for gm.intel.variance and gm.delivery.evidence — no DB, no network."""

import pytest

from gm.delivery.evidence import export_markdown
from gm.intel.variance import Window, fmt_rate, gate_verdict, prompt_verdicts, wilson

THRESHOLDS = {
    "movement": {
        "min_absolute_rate_gain": 0.30,
        "min_gain_over_control": 0.20,
        "min_samples_per_window": 9,
    },
    "gate": {"min_prompts_moved": 2, "max_control_drift": 0.15},
}


class TestWilson:
    def test_known_value_7_of_9(self):
        low, high = wilson(7, 9)
        assert low == pytest.approx(0.4527, abs=0.001)
        assert high == pytest.approx(0.9368, abs=0.001)

    def test_zero_of_9(self):
        low, high = wilson(0, 9)
        assert low == 0.0
        assert high == pytest.approx(0.2992, abs=0.001)

    def test_all_9_of_9(self):
        low, high = wilson(9, 9)
        assert low == pytest.approx(0.7008, abs=0.001)
        assert high == 1.0

    def test_n_zero(self):
        assert wilson(0, 0) == (0.0, 1.0)

    def test_bounds_clamped(self):
        for k, n in [(0, 3), (3, 3), (1, 100), (99, 100)]:
            low, high = wilson(k, n)
            assert 0.0 <= low <= high <= 1.0


def test_fmt_rate():
    assert fmt_rate(7, 9) == "7/9"
    assert fmt_rate(0, 0) == "0/0"


def test_window_rate():
    assert Window(7, 9).rate == pytest.approx(7 / 9)
    assert Window(0, 0).rate == 0.0


def test_prompt_verdicts_insufficiency():
    windows = {
        "p1": (Window(1, 9), Window(7, 9)),
        "p2": (Window(1, 5), Window(7, 9)),  # before window too small
        "p3": (Window(1, 9), Window(4, 8)),  # after window too small
    }
    verdicts = {v.prompt_id: v for v in prompt_verdicts(windows, THRESHOLDS)}
    assert verdicts["p1"].sufficient
    assert not verdicts["p2"].sufficient
    assert not verdicts["p3"].sufficient
    assert verdicts["p1"].gain == pytest.approx(6 / 9)
    assert verdicts["p1"].ci_after == wilson(7, 9)
    assert verdicts["p1"].ci_before == wilson(1, 9)


class TestGateVerdict:
    def test_pass(self):
        treatment = {
            "p1": (Window(1, 9), Window(7, 9)),  # gain ~0.67
            "p2": (Window(0, 9), Window(5, 9)),  # gain ~0.56
            "p3": (Window(2, 9), Window(2, 9)),  # no movement
        }
        v = gate_verdict(treatment, [0.0, 0.05, -0.05], THRESHOLDS)
        assert v.status == "PASS"
        assert set(v.moved_prompt_ids) == {"p1", "p2"}
        assert v.control_mean_drift == pytest.approx(0.1 / 3)
        assert len(v.details) == 3

    def test_fail_not_enough_moved(self):
        treatment = {
            "p1": (Window(1, 9), Window(7, 9)),
            "p2": (Window(2, 9), Window(3, 9)),  # gain too small
        }
        v = gate_verdict(treatment, [0.0], THRESHOLDS)
        assert v.status == "FAIL"
        assert v.moved_prompt_ids == ["p1"]
        assert any("required 2" in r for r in v.reasons)

    def test_insufficient_window_cannot_move(self):
        treatment = {"p1": (Window(0, 4), Window(4, 4))}  # huge gain, tiny n
        v = gate_verdict(treatment, [0.0], THRESHOLDS)
        assert v.moved_prompt_ids == []
        assert v.status == "FAIL"

    def test_gain_over_control_required(self):
        # gain 0.44 clears absolute threshold but control mean 0.30 eats the margin
        treatment = {
            "p1": (Window(1, 9), Window(5, 9)),
            "p2": (Window(1, 9), Window(5, 9)),
        }
        v = gate_verdict(treatment, [0.30, 0.30], THRESHOLDS)
        # drift 0.30 > 0.15 also trips INCONCLUSIVE — assert nothing "moved"
        assert v.moved_prompt_ids == []

    def test_inconclusive_on_control_drift(self):
        treatment = {
            "p1": (Window(1, 9), Window(8, 9)),
            "p2": (Window(0, 9), Window(7, 9)),
        }
        v = gate_verdict(treatment, [0.4, -0.4], THRESHOLDS)
        assert v.status == "INCONCLUSIVE"
        assert v.control_mean_drift == pytest.approx(0.4)
        assert any("FAIL" in r for r in v.reasons)
        assert any("drift" in r for r in v.reasons)

    def test_empty_control_gains(self):
        treatment = {
            "p1": (Window(1, 9), Window(7, 9)),
            "p2": (Window(0, 9), Window(6, 9)),
        }
        v = gate_verdict(treatment, [], THRESHOLDS)
        assert v.status == "PASS"  # drift treated as 0.0
        assert v.control_mean_drift == 0.0
        assert "no control data" in v.reasons


def _minimal_report() -> dict:
    return {
        "site": {"domain": "clinic.example", "is_control": False},
        "window_before": {"label": "baseline", "run_ids": ["r1", "r2", "r3"],
                          "date_range": "2026-06-01 to 2026-06-15"},
        "window_after": {"label": "treatment", "run_ids": ["r4", "r5", "r6"],
                         "date_range": "2026-06-16 to 2026-06-30"},
        "prompts": [
            {
                "prompt_text": "best med spa in dubai marina",
                "engine_breakdown": {
                    "openai": {"before": Window(0, 3), "after": Window(3, 3)},
                    "perplexity": {"before": Window(1, 3), "after": Window(2, 3)},
                    "gemini": {"before": Window(0, 3), "after": Window(2, 3)},
                },
                "pooled": {
                    "prompt_id": "p1",
                    "before": Window(1, 9),
                    "after": Window(7, 9),
                    "gain": 6 / 9,
                    "ci_before": wilson(1, 9),
                    "ci_after": wilson(7, 9),
                    "sufficient": True,
                },
            },
        ],
        "gate": {
            "status": "PASS",
            "moved_prompt_ids": ["p1"],
            "control_mean_drift": 0.03,
            "reasons": ["1 prompt(s) moved, required 1"],
        },
        "controls": [{"domain": "control.example", "gain": 0.05}],
        "levers": [
            {"applied_at": "2026-06-16", "lever_class": "onsite_fix",
             "description": "FAQ schema + answers page"},
        ],
        "raw_refs": ["raw/r4/p1-openai-0.json", "raw/r4/p1-openai-1.json"],
        "panel_hash": "abc123",
        "thresholds_status": "LOCKED 2026-07-05",
        "generated_at": "2026-07-03T12:00:00+00:00",
    }


class TestExportMarkdown:
    def test_minimal_report_renders(self):
        md = export_markdown(_minimal_report())
        assert isinstance(md, str) and md

    def test_contains_verdict_and_status(self):
        md = export_markdown(_minimal_report())
        assert "## Verdict: **PASS**" in md

    def test_contains_claim_ceiling(self):
        md = export_markdown(_minimal_report())
        assert "movement vs control, lever unattributed" in md

    def test_contains_rate_phrasing(self):
        md = export_markdown(_minimal_report())
        assert "named in 7/9 runs, was 1/9" in md

    def test_contains_prereg_status_and_hash(self):
        md = export_markdown(_minimal_report())
        assert "LOCKED 2026-07-05" in md
        assert "abc123" in md
        assert "2026-07-03T12:00:00+00:00" in md

    def test_contains_engine_breakdown_and_controls(self):
        md = export_markdown(_minimal_report())
        assert "| openai | 3/3 | 0/3 |" in md
        assert "perplexity" in md and "gemini" in md
        assert "control.example" in md
        assert "+0.05" in md

    def test_contains_lever_and_raw_ref_appendices(self):
        md = export_markdown(_minimal_report())
        assert "Lever log" in md
        assert "FAQ schema + answers page" in md
        assert "2 raw engine response(s)" in md

    def test_deterministic(self):
        assert export_markdown(_minimal_report()) == export_markdown(_minimal_report())

    def test_inconclusive_status_rendered(self):
        report = _minimal_report()
        report["gate"] = {
            "status": "INCONCLUSIVE",
            "moved_prompt_ids": [],
            "control_mean_drift": 0.21,
            "reasons": ["control drift 0.21 > 0.15 — muddy readout, counts as FAIL"],
        }
        md = export_markdown(report)
        assert "## Verdict: **INCONCLUSIVE**" in md
        assert "counts as FAIL" in md

    def test_no_exceptions_on_sparse_report(self):
        md = export_markdown({"generated_at": "2026-07-03T00:00:00+00:00"})
        assert "Evidence log" in md
        assert "movement vs control, lever unattributed" in md
