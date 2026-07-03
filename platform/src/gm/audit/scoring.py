"""Deterministic scoring core — port of aeo-seo-auditor-fable service/scoring.py.

WHY THIS MODULE EXISTS (inherited from the source verbatim in spirit): the LLM
CLASSIFIES each check (pass / warn / fail / na / inconclusive); Python GRADES
deterministically from those classifications. The same findings must always
produce byte-identical scores, and no forged or malformed model output may ever
reach persistence or rendering.

There is exactly ONE weight table and ONE grade table, and they live here.

Adaptations from the source (documented, everything else is a faithful port):
  - Input shape is our audit_findings rows: dicts with check_id / check_version
    / status / badge (plus whatever else the row carries). The audit-level dict
    of the source is replaced by an explicit `gate_state` argument
    (audits.gate_state) and a Registry for per-check weights and categories.
  - Within a section, checks are weighted by their registry weight (the source
    weighted all checks in a section equally; a registry weight of 1 for every
    check reproduces the source exactly).
  - `status` gains 'inconclusive' (our migration enum); it is treated like 'na'
    for scoring — excluded from the applicable pool.
  - DRIFT NOTE: the Phase B contract names a "demand-capture headline score"
    via `compute_demand_capture`, but NO function of that name exists anywhere
    in the source repos. The closest analog is fable's
    `combined_directional_score` (PCR*0.80 + BAP*0.20, informational only,
    never folded into the letter grade). That computation is ported here under
    the contract's name.

Pure functions; stdlib only; no DB, no network.
"""

from __future__ import annotations

import math
import re
from typing import Any

from gm.audit.registry import Registry

# ---------------------------------------------------------------------------
# CANONICAL TABLES — the only copies in the codebase
# ---------------------------------------------------------------------------

# THE one explicit mapping between registry category letters (A-J, as used in
# check_ids like "A-01") and fable's canonical section_scores keys. Fable's
# section keys differ from bare letters, so per the Phase B contract the
# translation lives in exactly this dict and nowhere else.
SECTION_KEYS: dict[str, str] = {
    "A": "A_technical",
    "B": "B_performance",
    "C": "C_onpage",
    "D": "D_schema",
    "E": "E_aeo_discovery",
    "F": "F_aeo_extraction",
    "G": "G_aeo_trust",
    "H": "H_aeo_selection",
    "I": "I_geo",
    "J": "J_entity",
}

# Page Citation Readiness (PCR) section weights. Canonical per fable's
# scoring-rubric. Sums to exactly 1.00. Section I (GEO) is intentionally
# EXCLUDED from PCR — it feeds Brand AI Presence (BAP) instead. PCR is the
# deterministic, page-fixable headline number; BAP is directional and reported
# separately, never folded into the letter grade.
PCR_WEIGHTS: dict[str, float] = {
    "A_technical": 0.16,
    "B_performance": 0.10,
    "C_onpage": 0.13,
    "D_schema": 0.16,
    "E_aeo_discovery": 0.13,
    "F_aeo_extraction": 0.13,
    "G_aeo_trust": 0.08,
    "H_aeo_selection": 0.08,
    "J_entity": 0.03,
}
assert abs(sum(PCR_WEIGHTS.values()) - 1.0) < 1e-9, "PCR weights must sum to 1.0"

# Brand AI Presence (BAP) sub-weights, grouped by section-I check NUMBER.
# Fable keyed these on ids like "I1"; our registry ids are "I-01" — both forms
# normalize to the integer via _BAP_ID_RE. Sums to 1.00.
BAP_GROUPS: dict[str, tuple[list[int], float]] = {
    "presence": ([1, 2, 8], 0.40),
    "accuracy": ([3, 4, 7], 0.35),
    "favorability": ([5, 6], 0.25),
}
assert abs(sum(w for _, w in BAP_GROUPS.values()) - 1.0) < 1e-9, "BAP weights must sum to 1.0"

# The ONE grade table. Monotonic, 9 grades + INCONCLUSIVE. Grade is derived
# from PCR (the deterministic number), never from the blended figure.
# (min_inclusive_score, grade) — evaluated top-down.
GRADE_TABLE: list[tuple[float, str]] = [
    (95.0, "A+"),
    (85.0, "A"),
    (80.0, "B+"),
    (75.0, "B"),
    (68.0, "C+"),
    (60.0, "C"),
    (53.0, "D+"),
    (45.0, "D"),
    (0.0, "F"),
]

VALID_GRADES = frozenset([g for _, g in GRADE_TABLE] + ["INCONCLUSIVE"])

# Our audit_findings status enum (002_phase_b_audits.sql). 'inconclusive' is
# new vs fable and scores like 'na' (excluded from the applicable pool).
VALID_STATUSES = frozenset({"pass", "warn", "fail", "na", "inconclusive"})

# Points per gradeable status (fable: pass=1.0, warn=0.5, fail=0.0).
_STATUS_POINTS = {"pass": 1.0, "warn": 0.5, "fail": 0.0}

# gate_state values meaning the probe never reached real page content, so no
# numeric grade is defensible (the redirect-incident failure mode where a
# healthy-but-misprobed page scored an F). Fable's TRANSPORT_INCONCLUSIVE set,
# plus our audits.gate_state vocabulary ('transport_inconclusive',
# 'robots_blocked', 'page_missing').
UNGRADEABLE_GATE_STATES = frozenset({
    "transport_inconclusive", "unresolved_redirect", "bot_blocked",
    "http_error", "fetch_failed", "robots_blocked", "page_missing",
})

_LEADING_LETTER = re.compile(r"^([A-Za-z])")
_BAP_ID_RE = re.compile(r"^I[-_ ]?0*(\d+)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def grade_for(score: float | None) -> str:
    """Deterministic letter grade for a 0-100 PCR score."""
    if score is None:
        return "INCONCLUSIVE"
    s = _clamp(score)
    for threshold, grade in GRADE_TABLE:
        if s >= threshold:
            return grade
    return "F"


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _num_or_none(x: Any) -> float | None:
    """Coerce a value to a finite float in [0,100], or None. Never raises.

    This is the choke point that stops a non-numeric or forged 'score' (the
    stored-XSS / score-forgery vector of the source incident) from surviving
    into persistence or rendering.
    """
    if x is None:
        return None
    if isinstance(x, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(x, (int, float)):
        return _clamp(float(x)) if math.isfinite(float(x)) else None
    if isinstance(x, str):
        m = re.search(r"-?\d+(?:\.\d+)?", x)
        if not m:
            return None
        try:
            return _clamp(float(m.group(0)))
        except ValueError:
            return None
    return None


def _section_of(check_id: str, registry: Registry) -> str | None:
    """Category letter for a finding: registry category first, else the
    leading letter of the check_id."""
    letter = registry.category_of(check_id)
    if letter in SECTION_KEYS:
        return letter
    m = _LEADING_LETTER.match(check_id)
    if m and m.group(1).upper() in SECTION_KEYS:
        return m.group(1).upper()
    return None


def compute_demand_capture(pcr: float | None, bap: float | None) -> float | None:
    """Demand-capture headline score (contract name).

    Port of fable's `combined_directional_score`: PCR*0.80 + BAP*0.20 with BAP
    falling back to PCR when unavailable. Informational / directional only —
    the letter grade is ALWAYS derived from PCR alone, and this number is
    never folded back into it. None when there is no PCR to anchor it.
    """
    p = _num_or_none(pcr)
    if p is None:
        return None
    b = _num_or_none(bap)
    return round(p * 0.80 + (b if b is not None else p) * 0.20, 1)


# ---------------------------------------------------------------------------
# VALIDATION — neutralize forged values before anything is counted
# ---------------------------------------------------------------------------

def _validate(findings: list[dict], registry: Registry) -> tuple[list[dict], list[str]]:
    """Normalize findings and collect human-readable notes about repairs."""
    clean: list[dict] = []
    notes: list[str] = []
    for f in findings or []:
        if not isinstance(f, dict):
            notes.append("dropped non-dict finding")
            continue
        cid = str(f.get("check_id") or "").strip()
        if not cid:
            notes.append("dropped finding with missing check_id")
            continue
        check = registry.checks.get(cid)
        if check is None:
            notes.append(f"dropped unknown check_id {cid!r}")
            continue

        out = dict(f)
        out["check_id"] = cid

        raw_status = f.get("status")
        status = raw_status.strip().lower() if isinstance(raw_status, str) else ""
        if status not in VALID_STATUSES:
            notes.append(f"{cid}: status {raw_status!r} out of enum -> 'na'")
            status = "na"
        out["status"] = status

        version = f.get("check_version")
        if isinstance(version, bool) or not isinstance(version, int):
            coerced = _num_or_none(version)
            if coerced is not None and float(coerced).is_integer():
                version = int(coerced)
            else:
                version = check.get("check_version")
                notes.append(f"{cid}: check_version repaired from registry")
        out["check_version"] = version

        # The registry is authoritative for the badge — a forged badge on a
        # finding row is overwritten, not trusted.
        reg_badge = check.get("badge")
        if f.get("badge") != reg_badge:
            if f.get("badge") is not None:
                notes.append(f"{cid}: badge {f.get('badge')!r} overridden from registry")
            out["badge"] = reg_badge

        clean.append(out)
    return clean, notes


def validate_findings(findings: list[dict], registry: Registry) -> list[dict]:
    """Public validator: neutralizes non-numeric/forged values (statuses out of
    enum -> 'na', badges rewritten from the registry, check_version repaired)
    and DROPS findings whose check_id is unknown to the registry. Never raises.
    """
    clean, _ = _validate(findings, registry)
    return clean


# ---------------------------------------------------------------------------
# CORE: recompute scores from per-check statuses
# ---------------------------------------------------------------------------

def _compute_bap(findings: list[dict]) -> tuple[float | None, str]:
    """Brand AI Presence from GEO (section I) checks. Directional — returns a
    confidence tag reflecting how many I-checks were actually gradeable.
    (Fable counted group members present at any status; we count only
    pass/warn/fail — the stricter, more honest coverage figure.)"""
    by_num: dict[int, str] = {}
    for f in findings:
        m = _BAP_ID_RE.match(str(f.get("check_id") or "").strip())
        if m:
            by_num[int(m.group(1))] = str(f.get("status") or "na")

    def group_score(nums: list[int]) -> float | None:
        vals = [_STATUS_POINTS[by_num[n]] for n in nums
                if by_num.get(n) in _STATUS_POINTS]
        return (sum(vals) / len(vals) * 100) if vals else None

    weighted = 0.0
    weight_sum = 0.0
    gradeable = 0
    for nums, w in BAP_GROUPS.values():
        gs = group_score(nums)
        if gs is not None:
            weighted += gs * w
            weight_sum += w
            gradeable += sum(1 for n in nums if by_num.get(n) in _STATUS_POINTS)

    if weight_sum == 0:
        return None, "none"
    bap = round(weighted / weight_sum, 1)
    # Confidence by coverage: BAP rides on live SERP/model judgment and is noisy.
    conf = "low" if gradeable < 3 else ("medium" if gradeable < 6 else "high")
    return bap, conf


def recompute_scores(findings: list[dict], registry: Registry, gate_state: str) -> dict:
    """Compute the full scoring block deterministically from per-check statuses.

    Within a section, checks are weighted by their registry weight: pass=1.0,
    warn=0.5, fail=0.0 points; na/inconclusive excluded (N/A renormalization).
    Sections combine into PCR via PCR_WEIGHTS, renormalized over applicable
    sections. A transport-inconclusive gate_state refuses to grade. Pure
    function of its inputs; never raises.
    """
    clean, notes = _validate(findings, registry)

    counts: dict[str, dict[str, int]] = {
        key: {"pass": 0, "warn": 0, "fail": 0, "na": 0, "inconclusive": 0}
        for key in SECTION_KEYS.values()
    }
    points: dict[str, float] = {key: 0.0 for key in SECTION_KEYS.values()}
    weight_pool: dict[str, float] = {key: 0.0 for key in SECTION_KEYS.values()}

    for f in clean:
        letter = _section_of(f["check_id"], registry)
        if letter is None:
            notes.append(f"{f['check_id']}: no A-J section -> excluded")
            continue
        key = SECTION_KEYS[letter]
        status = f["status"]
        counts[key][status] += 1
        if status in _STATUS_POINTS:
            w = registry.weight_of(f["check_id"])
            points[key] += _STATUS_POINTS[status] * w
            weight_pool[key] += w

    section_scores: dict[str, float | None] = {
        key: (round(points[key] / weight_pool[key] * 100, 1) if weight_pool[key] > 0 else None)
        for key in SECTION_KEYS.values()
    }

    # PCR — weighted over applicable, non-GEO sections; renormalized so a
    # section with no applicable checks doesn't drag the denominator.
    weighted = 0.0
    weight_sum = 0.0
    for key, w in PCR_WEIGHTS.items():
        v = section_scores.get(key)
        if v is not None:
            weighted += v * w
            weight_sum += w
    pcr = round(weighted / weight_sum, 1) if weight_sum > 0 else None

    # BAP — directional GEO signal, computed separately, NEVER in the grade.
    bap, bap_conf = _compute_bap(clean)

    gate = str(gate_state or "").strip().lower()
    gate_refuses = gate in UNGRADEABLE_GATE_STATES

    base = {
        "registry_version": registry.version,
        "gate_state": gate,
        "section_scores": section_scores,
        "section_counts": counts,
        "brand_ai_presence": bap,
        "brand_ai_presence_confidence": bap_conf,
        "grade_basis": "page_citation_readiness",
        "computed_by": "runtime-deterministic",
        "validation_notes": notes,
    }

    if gate_refuses or pcr is None:
        reason = (
            f"gate_state {gate!r} — content not reached, refusing to grade"
            if gate_refuses
            else "no applicable checks (nothing gradeable)"
        )
        return {
            **base,
            "page_citation_readiness": None,
            "demand_capture": None,
            "overall_score": None,
            "overall_grade": "INCONCLUSIVE",
            "inconclusive": True,
            "inconclusive_reason": reason,
        }

    return {
        **base,
        "page_citation_readiness": pcr,
        "demand_capture": compute_demand_capture(pcr, bap),
        "overall_score": pcr,
        "overall_grade": grade_for(pcr),
        "inconclusive": False,
    }
