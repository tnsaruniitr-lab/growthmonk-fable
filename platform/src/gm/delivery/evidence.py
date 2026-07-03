"""Evidence-log markdown exporter. Pure formatting: report dict in, GFM string out.

Deterministic for a given report — generated_at comes in the report dict, no clock
reads here. Windows/verdicts may arrive as dataclasses or plain dicts (e.g. after a
JSON round-trip), so field access is duck-typed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gm.intel.variance import fmt_rate, wilson

CLAIM_CEILING = "movement vs control, lever unattributed"


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _kn(win: Any) -> tuple[int, int]:
    return int(_get(win, "k", 0)), int(_get(win, "n", 0))


def _ci(win: Any, provided: Any = None) -> str:
    low, high = provided if provided is not None else wilson(*_kn(win))
    return f"{low:.2f}–{high:.2f}"


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _window_line(name: str, win: Any) -> str:
    label = _get(win, "label", "?")
    run_ids = _get(win, "run_ids", []) or []
    date_range = _get(win, "date_range", "?")
    return f"- **{name}**: {label} — {len(run_ids)} run(s), {date_range}"


def export_markdown(report: dict) -> str:
    site = report.get("site", {})
    gate = report.get("gate", {})
    prompts = report.get("prompts", [])
    controls = report.get("controls", [])
    levers = report.get("levers", [])

    domain = _get(site, "domain", "unknown")
    status = _get(gate, "status", "UNKNOWN")
    reasons = _get(gate, "reasons", []) or []
    moved = _get(gate, "moved_prompt_ids", []) or []
    drift = _get(gate, "control_mean_drift", 0.0)

    lines: list[str] = []
    lines.append(f"# Evidence log — {domain}")
    if _get(site, "is_control", False):
        lines.append("")
        lines.append("> Control site — no levers attributed here.")
    lines.append("")
    lines.append(f"Generated: {report.get('generated_at', 'unknown')}")
    lines.append(f"Panel hash: `{report.get('panel_hash', 'unknown')}`")
    lines.append("")
    lines.append(f"## Verdict: **{status}**")
    lines.append("")
    lines.append(
        f"Prompts moved: {len(moved)} ({', '.join(moved) if moved else 'none'}). "
        f"Control mean drift: {drift:.2f}."
    )
    for reason in reasons:
        lines.append(f"- {reason}")
    lines.append("")
    lines.append(f"Pre-registration status: {report.get('thresholds_status', 'UNKNOWN')}")
    lines.append("")
    lines.append(f"**Claim ceiling: {CLAIM_CEILING}.**")
    lines.append("")
    lines.append("## Measurement windows")
    lines.append("")
    lines.append(_window_line("Before", report.get("window_before", {})))
    lines.append(_window_line("After", report.get("window_after", {})))
    lines.append("")
    lines.append("## Per-prompt results (pooled across engines)")
    lines.append("")
    lines.append("| Prompt | Result | Wilson CI after | Wilson CI before | Gain | Sufficient n |")
    lines.append("|---|---|---|---|---|---|")
    for p in prompts:
        pooled = _get(p, "pooled", {})
        before, after = _get(pooled, "before", {}), _get(pooled, "after", {})
        bk, bn = _kn(before)
        ak, an = _kn(after)
        phrase = f"named in {fmt_rate(ak, an)} runs, was {fmt_rate(bk, bn)}"
        lines.append(
            f"| {_cell(_get(p, 'prompt_text', '?'))} "
            f"| {phrase} "
            f"| {_ci(after, _get(pooled, 'ci_after'))} "
            f"| {_ci(before, _get(pooled, 'ci_before'))} "
            f"| {_get(pooled, 'gain', 0.0):+.2f} "
            f"| {'yes' if _get(pooled, 'sufficient', False) else 'no'} |"
        )
    lines.append("")
    lines.append("### Engine breakdown")
    lines.append("")
    lines.append("| Prompt | Engine | After | Before |")
    lines.append("|---|---|---|---|")
    for p in prompts:
        for engine, windows in sorted((_get(p, "engine_breakdown", {}) or {}).items()):
            bk, bn = _kn(_get(windows, "before", {}))
            ak, an = _kn(_get(windows, "after", {}))
            lines.append(
                f"| {_cell(_get(p, 'prompt_text', '?'))} | {engine} "
                f"| {fmt_rate(ak, an)} | {fmt_rate(bk, bn)} |"
            )
    lines.append("")
    lines.append("## Control domains")
    lines.append("")
    if controls:
        lines.append("| Domain | Gain |")
        lines.append("|---|---|")
        for c in controls:
            lines.append(f"| {_cell(_get(c, 'domain', '?'))} | {_get(c, 'gain', 0.0):+.2f} |")
    else:
        lines.append("No control-domain data.")
    lines.append("")
    lines.append("## Appendix A — Lever log")
    lines.append("")
    if levers:
        lines.append("| Applied | Class | Description |")
        lines.append("|---|---|---|")
        for lever in levers:
            lines.append(
                f"| {_get(lever, 'applied_at', '?')} | {_get(lever, 'lever_class', '?')} "
                f"| {_cell(_get(lever, 'description', ''))} |"
            )
    else:
        lines.append("No levers recorded for this window.")
    lines.append("")
    lines.append("## Appendix B — Raw sample references")
    lines.append("")
    raw_refs = report.get("raw_refs", []) or []
    lines.append(f"{len(raw_refs)} raw engine response(s) stored and referenced by this log.")
    for ref in raw_refs:
        lines.append(f"- `{ref}`")
    lines.append("")
    return "\n".join(lines)
