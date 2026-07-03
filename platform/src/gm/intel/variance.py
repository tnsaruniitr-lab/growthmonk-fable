"""Pure Gate-1 statistics: Wilson intervals, per-prompt verdicts, gate verdict.

No DB, no IO. Thresholds arrive as a parsed dict of ops/gate1-thresholds.yaml.
Samples with a recorded error are excluded upstream — every Window here is
error-free counts only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion; n=0 -> (0.0, 1.0)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def fmt_rate(k: int, n: int) -> str:
    return f"{k}/{n}"


@dataclass
class Window:
    k: int
    n: int

    @property
    def rate(self) -> float:
        return self.k / self.n if self.n else 0.0


@dataclass
class PromptVerdict:
    prompt_id: str
    before: Window
    after: Window
    gain: float
    ci_before: tuple[float, float]
    ci_after: tuple[float, float]
    sufficient: bool


def prompt_verdicts(
    windows: dict[str, tuple[Window, Window]], thresholds: dict
) -> list[PromptVerdict]:
    min_n = thresholds["movement"]["min_samples_per_window"]
    out: list[PromptVerdict] = []
    for prompt_id, (before, after) in windows.items():
        out.append(
            PromptVerdict(
                prompt_id=prompt_id,
                before=before,
                after=after,
                gain=after.rate - before.rate,
                ci_before=wilson(before.k, before.n),
                ci_after=wilson(after.k, after.n),
                sufficient=before.n >= min_n and after.n >= min_n,
            )
        )
    return out


@dataclass
class GateVerdict:
    status: str  # "PASS" | "FAIL" | "INCONCLUSIVE"
    moved_prompt_ids: list[str]
    control_mean_drift: float
    details: list[PromptVerdict]
    reasons: list[str]


def gate_verdict(
    treatment: dict[str, tuple[Window, Window]],
    control_gains: list[float],
    thresholds: dict,
) -> GateVerdict:
    """Gate-1 decision per the pre-registration.

    moved = sufficient AND gain >= movement.min_absolute_rate_gain
            AND (gain - mean(control_gains)) >= movement.min_gain_over_control.
    INCONCLUSIVE when mean(|control_gains|) > gate.max_control_drift — the
    pre-registration treats a muddy readout as FAIL, so the status string stays
    INCONCLUSIVE and a reasons entry spells out the FAIL semantics.
    """
    movement = thresholds["movement"]
    gate = thresholds["gate"]
    details = prompt_verdicts(treatment, thresholds)
    reasons: list[str] = []

    if control_gains:
        control_mean = sum(control_gains) / len(control_gains)
        drift = sum(abs(g) for g in control_gains) / len(control_gains)
    else:
        control_mean = 0.0
        drift = 0.0
        reasons.append("no control data")

    moved = [
        d.prompt_id
        for d in details
        if d.sufficient
        and d.gain >= movement["min_absolute_rate_gain"]
        and (d.gain - control_mean) >= movement["min_gain_over_control"]
    ]

    max_drift = gate["max_control_drift"]
    min_moved = gate["min_prompts_moved"]
    if drift > max_drift:
        status = "INCONCLUSIVE"
        reasons.append(
            f"control drift {drift:.2f} > {max_drift:.2f} — muddy readout, "
            "counts as FAIL per pre-registration"
        )
    elif len(moved) >= min_moved:
        status = "PASS"
        reasons.append(f"{len(moved)} prompt(s) moved, required {min_moved}")
    else:
        status = "FAIL"
        reasons.append(f"only {len(moved)} prompt(s) moved, required {min_moved}")

    return GateVerdict(
        status=status,
        moved_prompt_ids=moved,
        control_mean_drift=drift,
        details=details,
        reasons=reasons,
    )
