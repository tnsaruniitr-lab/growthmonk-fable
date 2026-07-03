"""Fix-verification / re-score / delta engine — port of fable service/delta.py.

THE PRODUCT LOOP (inherited rationale): an auditor's commercial value is not
the one-shot teardown — it is proving movement: "we told you to fix X; your
score went 62 → 81, 7 findings resolved, 1 regressed." This module diffs two
audits' findings by check_id and reports what resolved, persisted, newly
appeared, or regressed — plus score movement. Pure data work: deterministic,
no LLM, no DB, no network.

Adaptations from the source:
  - Inputs are our audit_findings row shapes (dicts with check_id /
    check_version / status / ...) rather than whole audit dicts; score
    movement comes from the optional audits.scores jsonb blobs.
  - ADR-13 comparability rule (new vs fable): a check_id present on BOTH
    sides is comparable IFF check_version matches on both sides. When the
    version changed, the check's meaning may have changed, so it is listed
    under `non_comparable` and NEVER counted as resolved/regressed/persisting.
  - Transition semantics follow fable's documented intent: `regressed` means
    was PASS, now fail/warn. (Fable's code accidentally also classified
    na→fail as regressed, contradicting its own docstring, which says
    "absent/na before" is a NEW issue; we implement the docstring.)
    'inconclusive' — our extra status — behaves like 'na'.
"""

from __future__ import annotations

from typing import Any

_BAD = frozenset({"fail", "warn"})
_NEUTRAL = frozenset({"na", "inconclusive", "absent"})


def _index(findings: list[dict]) -> dict[str, dict]:
    """Map check_id -> finding (last write wins on dupes, as in fable)."""
    out: dict[str, dict] = {}
    for f in findings or []:
        if isinstance(f, dict):
            cid = str(f.get("check_id") or "").strip()
            if cid:
                out[cid] = f
    return out


def _status(f: dict | None) -> str:
    if not f:
        return "absent"
    st = str(f.get("status") or "na").strip().lower()
    return st if st in ({"pass"} | _BAD | _NEUTRAL) else "na"


def _version(f: dict | None) -> int | None:
    if not f:
        return None
    v = f.get("check_version")
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _score(scores: dict | None) -> float | None:
    if not isinstance(scores, dict):
        return None
    v = scores.get("overall_score")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _grade(scores: dict | None) -> str | None:
    if not isinstance(scores, dict):
        return None
    g = scores.get("overall_grade")
    return g if isinstance(g, str) else None


def audit_delta(
    before: list[dict],
    after: list[dict],
    *,
    before_scores: dict | None = None,
    after_scores: dict | None = None,
) -> dict[str, Any]:
    """Diff two audits' findings (before → after) of the same page.

    Returns:
        {
          resolved:       [check_id, ...]  # was fail/warn, now pass (or gone-good)
          regressed:      [check_id, ...]  # was pass, now fail/warn
          new_issues:     [check_id, ...]  # absent/na/inconclusive before, now fail/warn
          persisting:     [check_id, ...]  # fail/warn on both sides
          non_comparable: [{check_id, before_version, after_version}, ...]  # ADR-13
          counts:         {...},
          score_delta:    {prior, current, change, grade_prior, grade_current},
          summary:        str,
        }

    Non-comparable checks (check_version differs between the two sides) are
    reported separately and never counted as resolved/regressed/persisting.
    """
    b_idx = _index(before)
    a_idx = _index(after)

    resolved: list[str] = []
    regressed: list[str] = []
    new_issues: list[str] = []
    persisting: list[str] = []
    non_comparable: list[dict] = []

    for cid in sorted(set(b_idx) | set(a_idx)):
        bf, af = b_idx.get(cid), a_idx.get(cid)
        if bf is not None and af is not None:
            bv, av = _version(bf), _version(af)
            if bv is None or av is None or bv != av:
                non_comparable.append(
                    {"check_id": cid, "before_version": bv, "after_version": av}
                )
                continue
        bs, as_ = _status(bf), _status(af)
        b_bad = bs in _BAD
        a_bad = as_ in _BAD
        if b_bad and not a_bad:
            resolved.append(cid)
        elif bs == "pass" and a_bad:
            regressed.append(cid)
        elif a_bad and bs in _NEUTRAL:
            new_issues.append(cid)
        elif b_bad and a_bad:
            persisting.append(cid)

    sp, sc = _score(before_scores), _score(after_scores)
    change = round(sc - sp, 1) if (sp is not None and sc is not None) else None

    direction = (
        "no prior score to compare" if change is None else
        f"up {change}" if change > 0 else
        f"down {abs(change)}" if change < 0 else
        "unchanged"
    )
    summary = (
        f"Score {direction}"
        + (f" ({sp} → {sc})" if sp is not None and sc is not None else "")
        + f". {len(resolved)} resolved, {len(regressed)} regressed, "
          f"{len(new_issues)} new, {len(persisting)} still open."
        + (
            f" {len(non_comparable)} not comparable (check_version changed)."
            if non_comparable else ""
        )
    )

    return {
        "resolved": resolved,
        "regressed": regressed,
        "new_issues": new_issues,
        "persisting": persisting,
        "non_comparable": non_comparable,
        "counts": {
            "resolved": len(resolved),
            "regressed": len(regressed),
            "new_issues": len(new_issues),
            "persisting": len(persisting),
            "non_comparable": len(non_comparable),
        },
        "score_delta": {
            "prior": sp,
            "current": sc,
            "change": change,
            "grade_prior": _grade(before_scores),
            "grade_current": _grade(after_scores),
        },
        "summary": summary,
    }
