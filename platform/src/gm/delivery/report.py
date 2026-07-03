"""Self-contained, evidence-badged HTML audit report (docs/phase-b-wave2-contracts.md).

Security posture: EVERY dynamic string goes through html.escape — findings
evidence quotes crawled page content, which is attacker-influenced, and the
scores jsonb could in principle carry forged strings. CSS class names are
never built from data directly: statuses/badges/fix_types outside a fixed
allowlist fall back to a neutral class (their text is still escaped and
shown). The document is strict-CSP compatible: inline CSS only, no scripts,
no remote fonts/images/fetches, plus an @media print stylesheet.

Pure function of its inputs (except the generated-at timestamp in the footer);
no DB, no network.
"""

from __future__ import annotations

import datetime as dt
import html
from typing import Any

from gm.audit.scoring import SECTION_KEYS

_SECTION_LABELS: dict[str, str] = {
    "A_technical": "Technical SEO",
    "B_performance": "Performance",
    "C_onpage": "On-page",
    "D_schema": "Schema markup",
    "E_aeo_discovery": "AEO — Discovery",
    "F_aeo_extraction": "AEO — Extraction",
    "G_aeo_trust": "AEO — Trust",
    "H_aeo_selection": "AEO — Selection",
    "I_geo": "GEO — Brand presence",
    "J_entity": "Entity consistency",
}

# Grouping order for the findings section. Registry fix_type vocabulary
# (gm.audit.registry.VALID_FIX_TYPES) ordered from "you can ship this today"
# to "out of your hands"; unknown values render after these, escaped.
_FIX_TYPE_ORDER = [
    "page_html",
    "schema",
    "content_restructure",
    "sitewide_template",
    "cms_constraint",
    "offpage_entity",
    "cannot_fix_from_page",
]
_FIX_TYPE_LABELS: dict[str, str] = {
    "page_html": "Page HTML fixes",
    "schema": "Schema markup fixes",
    "content_restructure": "Content restructuring",
    "sitewide_template": "Sitewide template fixes",
    "cms_constraint": "CMS-constrained fixes",
    "offpage_entity": "Off-page / entity work",
    "cannot_fix_from_page": "Not fixable from the page",
}
_UNSPECIFIED = "unspecified"

_BADGE_LABELS: dict[str, str] = {
    "hard_evidence": "hard evidence",
    "measured": "measured",
    "static_rule": "static rule",
    "comparative": "comparative",
    "heuristic": "heuristic",
    "model_judgment": "model judgment",
}

# Also the within-group sort order: failures first, passes last.
_STATUS_ORDER: dict[str, int] = {"fail": 0, "warn": 1, "inconclusive": 2, "na": 3, "pass": 4}

# Static stylesheet — no interpolation, ever. Class names referenced here are
# only attached from allowlists above.
_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font: 15px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #1c2430; margin: 0; background: #f5f6f8; }
main { max-width: 860px; margin: 0 auto; padding: 24px 20px 48px; }
header.report { background: #101828; color: #fff; border-radius: 10px; padding: 24px; }
header.report h1 { margin: 0 0 4px; font-size: 22px; }
header.report .meta { color: #cbd3e1; font-size: 13px; }
.headline { display: flex; gap: 28px; flex-wrap: wrap; margin-top: 18px; }
.headline .stat .label { font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
  color: #98a4b8; }
.headline .stat .value { font-size: 28px; font-weight: 700; }
section { margin-top: 28px; }
h2 { font-size: 17px; margin: 0 0 10px; }
h3 { font-size: 14px; margin: 22px 0 8px; }
table { border-collapse: collapse; width: 100%; background: #fff; border-radius: 8px;
  overflow: hidden; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #e7eaf0;
  font-size: 14px; }
th { background: #eef1f5; font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.finding { background: #fff; border: 1px solid #e7eaf0; border-left: 4px solid #c6ccd6;
  border-radius: 6px; padding: 10px 14px; margin: 8px 0; }
.finding.s-fail { border-left-color: #d92d20; }
.finding.s-warn { border-left-color: #f79009; }
.finding.s-pass { border-left-color: #12b76a; }
.finding.s-inconclusive, .finding.s-na { border-left-color: #98a2b3; }
.finding-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.finding-head code { font-size: 13px; background: #f2f4f7; padding: 1px 6px;
  border-radius: 4px; }
.pill, .chip { font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 600; }
.pill { text-transform: uppercase; letter-spacing: .04em; }
.pill.p-fail { background: #fee4e2; color: #b42318; }
.pill.p-warn { background: #fef0c7; color: #b54708; }
.pill.p-pass { background: #d1fadf; color: #027a48; }
.pill.p-na, .pill.p-inconclusive, .pill.p-unknown { background: #eaecf0; color: #475467; }
.chip { background: #eef4ff; color: #3538cd; border: 1px solid #c7d7fe; }
.chip.c-hard_evidence { background: #d1fadf; color: #05603a; border-color: #a6f4c5; }
.chip.c-measured { background: #e0f2fe; color: #026aa2; border-color: #b9e6fe; }
.chip.c-static_rule { background: #eef1f5; color: #364152; border-color: #d5dae1; }
.chip.c-comparative { background: #fdf2fa; color: #9e165f; border-color: #fcceee; }
.chip.c-heuristic { background: #fef0c7; color: #93370d; border-color: #fedf89; }
.chip.c-model_judgment { background: #ebe9fe; color: #5925dc; border-color: #d9d6fe; }
.chip.c-unknown { background: #eaecf0; color: #475467; border-color: #d0d5dd; }
p.note { margin: 8px 0 2px; font-size: 13.5px; color: #364152; white-space: pre-wrap;
  overflow-wrap: anywhere; }
footer { margin-top: 36px; color: #667085; font-size: 12px; border-top: 1px solid #e7eaf0;
  padding-top: 12px; }
@media print {
  body { background: #fff; font-size: 12px; }
  main { max-width: none; padding: 0; }
  header.report { color: #000; background: #fff; border: 1px solid #000; border-radius: 0; }
  header.report .meta, .headline .stat .label { color: #333; }
  .finding { break-inside: avoid; }
  a { color: inherit; text-decoration: none; }
}
"""


def _esc(value: Any) -> str:
    """html.escape for arbitrary values; None renders empty. The ONLY way any
    dynamic value enters the document."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _fmt_date(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d")
    return str(value) if value else ""


def _dict_or_empty(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _score_cell(value: Any) -> str:
    return "&mdash;" if value is None else _esc(value)


def _group_findings(findings: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        ft = f.get("fix_type")
        key = ft if isinstance(ft, str) and ft else _UNSPECIFIED
        groups.setdefault(key, []).append(f)

    ordered = [ft for ft in _FIX_TYPE_ORDER if ft in groups]
    ordered += sorted(k for k in groups if k not in _FIX_TYPE_ORDER and k != _UNSPECIFIED)
    if _UNSPECIFIED in groups:
        ordered.append(_UNSPECIFIED)

    def sort_key(f: dict) -> tuple[int, str]:
        return (_STATUS_ORDER.get(str(f.get("status")), 5), str(f.get("check_id") or ""))

    return [(ft, sorted(groups[ft], key=sort_key)) for ft in ordered]


def _finding_html(f: dict) -> str:
    status = str(f.get("status") or "")
    status_cls = status if status in _STATUS_ORDER else "unknown"  # allowlisted class only
    badge = str(f.get("badge") or "")
    badge_cls = badge if badge in _BADGE_LABELS else "unknown"
    badge_label = _BADGE_LABELS.get(badge, badge or "unbadged")
    evidence = f.get("evidence")
    note = evidence.get("note") if isinstance(evidence, dict) else evidence

    parts = [
        f'<article class="finding s-{status_cls}">',
        '<div class="finding-head">'
        f"<code>{_esc(f.get('check_id'))}</code>"
        f'<span class="pill p-{status_cls}">{_esc(status or "unknown")}</span>'
        f'<span class="chip c-{badge_cls}">{_esc(badge_label)}</span>'
        "</div>",
    ]
    if note:
        parts.append(f'<p class="note">{_esc(note)}</p>')
    parts.append("</article>")
    return "".join(parts)


def render_audit_html(audit: dict, findings: list[dict], site: dict) -> str:
    """Render one audit as a self-contained HTML document string."""
    audit = _dict_or_empty(audit)
    site = _dict_or_empty(site)
    scores = _dict_or_empty(audit.get("scores"))
    section_scores = _dict_or_empty(scores.get("section_scores"))
    section_counts = _dict_or_empty(scores.get("section_counts"))

    domain = site.get("domain_norm") or site.get("domain") or ""
    url = audit.get("url") or ""
    audited_on = _fmt_date(audit.get("finished_at") or audit.get("created_at"))
    grade = scores.get("overall_grade") or "INCONCLUSIVE"
    demand = scores.get("demand_capture")
    pcr = scores.get("page_citation_readiness")
    gate = audit.get("gate_state") or scores.get("gate_state") or ""
    generated = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")

    out: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="robots" content="noindex">',
        f"<title>AI Demand Capture audit — {_esc(domain)}</title>",
        f"<style>{_CSS}</style>",
        "</head><body><main>",
        # -- header ----------------------------------------------------------
        '<header class="report">',
        f"<h1>{_esc(domain)}</h1>",
        f'<div class="meta">{_esc(url)}</div>',
        f'<div class="meta">Audited {_esc(audited_on)}'
        + (f" &middot; gate: {_esc(gate)}" if gate else "")
        + "</div>",
        '<div class="headline">',
        '<div class="stat"><div class="label">Overall grade</div>'
        f'<div class="value">{_esc(grade)}</div></div>',
        '<div class="stat"><div class="label">AI Demand Capture</div>'
        f'<div class="value">{_score_cell(demand)}</div></div>',
        '<div class="stat"><div class="label">Citation readiness</div>'
        f'<div class="value">{_score_cell(pcr)}</div></div>',
        "</div></header>",
        # -- score table -----------------------------------------------------
        "<section><h2>Category scores</h2><table>",
        "<tr><th>Category</th><th>Score</th><th>Pass</th><th>Warn</th><th>Fail</th></tr>",
    ]

    for key in SECTION_KEYS.values():
        counts = _dict_or_empty(section_counts.get(key))
        out.append(
            f"<tr><td>{_esc(_SECTION_LABELS.get(key, key))}</td>"
            f'<td class="num">{_score_cell(section_scores.get(key))}</td>'
            f'<td class="num">{_esc(counts.get("pass", 0))}</td>'
            f'<td class="num">{_esc(counts.get("warn", 0))}</td>'
            f'<td class="num">{_esc(counts.get("fail", 0))}</td></tr>'
        )
    out.append("</table></section>")

    # -- findings grouped by fix_type -----------------------------------------
    out.append("<section><h2>Findings</h2>")
    grouped = _group_findings(findings)
    if not grouped:
        out.append("<p>No findings recorded for this audit.</p>")
    for fix_type, group in grouped:
        label = _FIX_TYPE_LABELS.get(fix_type, fix_type)
        out.append(f"<h3>{_esc(label)} ({len(group)})</h3>")
        out.extend(_finding_html(f) for f in group)
    out.append("</section>")

    # -- footer ----------------------------------------------------------------
    out.append(
        "<footer>"
        f"Check registry {_esc(audit.get('registry_version'))}"
        f" &middot; model {_esc(audit.get('model_version'))}"
        f" &middot; generated {_esc(generated)}"
        "</footer>"
    )
    out.append("</main></body></html>")
    return "".join(out)
