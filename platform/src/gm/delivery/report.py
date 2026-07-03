"""Self-contained, evidence-badged HTML audit report (docs/phase-b-wave2-contracts.md).

Security posture: EVERY dynamic string goes through html.escape — findings
evidence quotes crawled page content, which is attacker-influenced, and the
scores jsonb could in principle carry forged strings. CSS class names are
never built from data directly: statuses/badges/fix_types outside a fixed
allowlist fall back to a neutral class (their text is still escaped and
shown). Numeric values used inside style attributes are coerced to float and
clamped in Python before interpolation. The document is strict-CSP compatible:
inline CSS only, no scripts, no remote fonts/images/fetches, plus an
@media print stylesheet.

Pure function of its inputs (except the generated-at timestamp in the footer);
no DB, no network. `checks_meta` (optional) maps check_id -> registry check
dicts so findings can show human names, severity and fix templates.
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

_STATUS_ORDER: dict[str, int] = {"fail": 0, "warn": 1, "inconclusive": 2, "na": 3, "pass": 4}
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Static stylesheet — no interpolation, ever. Class names referenced here are
# only attached from allowlists; the single dynamic style attribute (bar
# widths) interpolates a Python-clamped float.
_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; background: #FBFAF7; color: #141B1E;
  font: 15px/1.55 "Iowan Old Style", Charter, "Palatino Linotype", Georgia, serif; }
main { max-width: 880px; margin: 0 auto; padding: 40px 24px 64px; }
.sans { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
code, .mono, .num { font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace; }

/* Masthead — ledger double rule + grade stamp */
.masthead { border-top: 3px double #141B1E; border-bottom: 1px solid #141B1E;
  padding: 18px 0 20px; display: flex; gap: 24px; align-items: flex-start;
  justify-content: space-between; flex-wrap: wrap; }
.eyebrow { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 11px;
  letter-spacing: .22em; text-transform: uppercase; color: #0E5F66; font-weight: 700;
  margin: 0 0 8px; }
.masthead h1 { font-size: 34px; line-height: 1.1; margin: 0 0 6px; font-weight: 700;
  letter-spacing: -.01em; overflow-wrap: anywhere; }
.masthead .meta { font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 12.5px; color: #5B665F; overflow-wrap: anywhere; }
.stamp { flex: none; width: 108px; border: 2.5px solid #0E5F66; border-radius: 8px;
  padding: 10px 8px 9px; text-align: center; color: #0E5F66;
  transform: rotate(2deg); background: rgba(14,95,102,.035); }
.stamp .g { font-size: 40px; font-weight: 800; line-height: 1;
  font-family: -apple-system, "Segoe UI", Roboto, sans-serif; letter-spacing: -.02em; }
.stamp .s { font-family: "SF Mono", ui-monospace, Menlo, monospace; font-size: 12px;
  margin-top: 4px; }
.stamp .l { font-size: 8.5px; letter-spacing: .18em; text-transform: uppercase;
  font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-weight: 700;
  margin-top: 5px; border-top: 1px solid rgba(14,95,102,.4); padding-top: 4px; }
.stamp.inconclusive { border-color: #8A948F; color: #5B665F; background: #F1F0EA; }

.statrow { display: flex; gap: 34px; flex-wrap: wrap; padding: 16px 0 4px; }
.stat .label { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 10.5px;
  text-transform: uppercase; letter-spacing: .14em; color: #8A948F; font-weight: 700; }
.stat .value { font-family: "SF Mono", ui-monospace, Menlo, monospace; font-size: 24px;
  font-weight: 600; font-variant-numeric: tabular-nums; }

section { margin-top: 40px; }
h2 { font-size: 20px; margin: 0 0 4px; letter-spacing: -.005em; }
h2 + .sub { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 12.5px;
  color: #5B665F; margin: 0 0 16px; }
h3 { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 12px;
  text-transform: uppercase; letter-spacing: .12em; color: #5B665F; margin: 28px 0 10px; }

/* Fix queue — the lead section */
ol.queue { list-style: none; counter-reset: q; margin: 0; padding: 0; }
ol.queue li { counter-increment: q; display: flex; gap: 14px; padding: 12px 2px;
  border-bottom: 1px solid #E4E1D8; align-items: baseline; }
ol.queue li::before { content: counter(q, decimal-leading-zero);
  font-family: "SF Mono", ui-monospace, Menlo, monospace; font-size: 13px; color: #0E5F66;
  font-weight: 700; flex: none; width: 26px; }
.q-name { font-weight: 700; }
.q-detail { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 13px;
  color: #3C4740; margin-top: 2px; overflow-wrap: anywhere; }
.q-tags { margin-left: auto; flex: none; display: flex; gap: 6px; align-items: center; }

/* Category ledger bars */
.ledger { border-top: 1px solid #141B1E; }
.lrow { display: grid; grid-template-columns: 190px 1fr 52px; gap: 12px; align-items: center;
  padding: 8px 0; border-bottom: 1px solid #E4E1D8;
  font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 13px; }
.lrow .cat { color: #3C4740; }
.bar { height: 8px; background: #EEECE4; border-radius: 4px; overflow: hidden; }
.bar > i { display: block; height: 100%; border-radius: 4px; background: #8A948F; }
.bar > i.good { background: #2E7146; }
.bar > i.mid { background: #B07C10; }
.bar > i.bad { background: #B3402F; }
.lrow .num { text-align: right; font-size: 13px; font-variant-numeric: tabular-nums; }
.lrow .counts { color: #8A948F; font-size: 11px; text-align: right; }

/* Findings */
.finding { border: 1px solid #E4E1D8; border-left: 4px solid #C9C5B8; border-radius: 6px;
  background: #fff; padding: 12px 16px; margin: 10px 0; }
.finding.s-fail { border-left-color: #B3402F; }
.finding.s-warn { border-left-color: #B07C10; }
.finding.s-pass { border-left-color: #2E7146; }
.finding.s-inconclusive, .finding.s-na, .finding.s-unknown { border-left-color: #C9C5B8; }
.finding-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
  font-family: -apple-system, "Segoe UI", Roboto, sans-serif; }
.finding-head code { font-size: 12px; color: #5B665F; }
.f-name { font-family: "Iowan Old Style", Charter, Georgia, serif; font-size: 15.5px;
  font-weight: 700; }
.pill, .chip { font-size: 10.5px; padding: 2px 9px; border-radius: 999px; font-weight: 700;
  font-family: -apple-system, "Segoe UI", Roboto, sans-serif; letter-spacing: .03em; }
.pill { text-transform: uppercase; }
.pill.p-fail { background: #F7E4E0; color: #8C2F22; }
.pill.p-warn { background: #F5EBD3; color: #7C570B; }
.pill.p-pass { background: #E2EFE5; color: #235C37; }
.pill.p-na, .pill.p-inconclusive, .pill.p-unknown { background: #EEECE4; color: #5B665F; }
.chip { background: #E7EEEE; color: #0B4A50; border: 1px solid #C6D8D9; }
.chip.c-hard_evidence { background: #E2EFE5; color: #1F5230; border-color: #BFDCC8; }
.chip.c-measured { background: #E3EDF5; color: #1F4E75; border-color: #C2D8E8; }
.chip.c-static_rule { background: #EEECE4; color: #4A5450; border-color: #DBD7CA; }
.chip.c-comparative { background: #F2E7F0; color: #6E3260; border-color: #E0C8DC; }
.chip.c-heuristic { background: #F5EBD3; color: #7C570B; border-color: #E8D6A8; }
.chip.c-model_judgment { background: #E9E6F4; color: #4A3B82; border-color: #D3CDE9; }
.chip.c-unknown { background: #EEECE4; color: #5B665F; border-color: #DBD7CA; }
p.note { margin: 9px 0 2px; font-size: 13.5px; color: #3C4740; white-space: pre-wrap;
  overflow-wrap: anywhere; font-family: -apple-system, "Segoe UI", Roboto, sans-serif; }
.cites { margin: 8px 0 0; padding: 8px 0 0; border-top: 1px dashed #E4E1D8;
  font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 12px; color: #5B665F; }
.cites b { color: #3C4740; }
.cites a { color: #0E5F66; text-decoration: none; border-bottom: 1px solid #C6D8D9; }

footer { margin-top: 48px; border-top: 3px double #141B1E; padding-top: 12px;
  font-family: -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 11.5px;
  color: #5B665F; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
footer .claim { font-style: italic; }

@media print {
  body { background: #fff; font-size: 12px; }
  main { max-width: none; padding: 0; }
  .finding, ol.queue li { break-inside: avoid; }
  .stamp { transform: none; }
  a { color: inherit; }
}
"""


def _esc(value: Any) -> str:
    """html.escape for arbitrary values; None renders empty. The ONLY way any
    dynamic value enters the document."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_date(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d")
    return str(value) if value else ""


def _dict_or_empty(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _score_cell(value: Any) -> str:
    n = _num(value)
    return "&mdash;" if n is None else _esc(round(n))


def _meta_for(checks_meta: dict | None, check_id: Any) -> dict:
    if not isinstance(checks_meta, dict):
        return {}
    meta = checks_meta.get(str(check_id))
    return meta if isinstance(meta, dict) else {}


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


def _fix_queue(findings: list[dict], checks_meta: dict | None, limit: int = 7) -> list[dict]:
    """Worst actionable items first: fails then warns, by severity, weight, id."""
    rows = []
    for f in findings or []:
        if not isinstance(f, dict) or f.get("status") not in ("fail", "warn"):
            continue
        if f.get("fix_type") == "cannot_fix_from_page":
            continue
        meta = _meta_for(checks_meta, f.get("check_id"))
        rows.append((
            _STATUS_ORDER.get(str(f.get("status")), 5),
            _SEVERITY_RANK.get(str(meta.get("severity")), 4),
            -(_num(meta.get("weight")) or 0),
            str(f.get("check_id") or ""),
            f,
            meta,
        ))
    rows.sort(key=lambda r: r[:4])
    return [{"finding": r[4], "meta": r[5]} for r in rows[:limit]]


def _citations_html(f: dict) -> str:
    cites = f.get("citations")
    if not isinstance(cites, list) or not cites:
        return ""
    parts = ['<div class="cites"><b>Why this matters:</b> ']
    links = []
    for c in cites[:3]:
        if not isinstance(c, dict):
            continue
        title = c.get("title") or c.get("id") or "source"
        org = c.get("source_org")
        url = str(c.get("source_url") or "")
        label = f"{_esc(org)} — {_esc(title)}" if org else _esc(title)
        if url.startswith("http://") or url.startswith("https://"):
            links.append(f'<a href="{_esc(url)}" rel="noopener">{label}</a>')
        else:
            links.append(label)
    parts.append(" &middot; ".join(links))
    parts.append("</div>")
    return "".join(parts) if links else ""


def _finding_html(f: dict, checks_meta: dict | None = None) -> str:
    status = str(f.get("status") or "")
    status_cls = status if status in _STATUS_ORDER else "unknown"  # allowlisted class only
    badge = str(f.get("badge") or "")
    badge_cls = badge if badge in _BADGE_LABELS else "unknown"
    badge_label = _BADGE_LABELS.get(badge, badge or "unbadged")
    evidence = f.get("evidence")
    note = evidence.get("note") if isinstance(evidence, dict) else evidence
    name = _meta_for(checks_meta, f.get("check_id")).get("name")

    parts = [
        f'<article class="finding s-{status_cls}">',
        '<div class="finding-head">'
        f"<code>{_esc(f.get('check_id'))}</code>"
        + (f'<span class="f-name">{_esc(name)}</span>' if name else "")
        + f'<span class="pill p-{status_cls}">{_esc(status or "unknown")}</span>'
        f'<span class="chip c-{badge_cls}">{_esc(badge_label)}</span>'
        "</div>",
    ]
    if note:
        parts.append(f'<p class="note">{_esc(note)}</p>')
    parts.append(_citations_html(f))
    parts.append("</article>")
    return "".join(parts)


def _bar_html(score: Any) -> str:
    n = _num(score)
    if n is None:
        return '<div class="bar"></div>'
    width = max(0.0, min(100.0, n))  # clamped float — the only non-_esc interpolation
    cls = "good" if width >= 80 else ("mid" if width >= 60 else "bad")
    return f'<div class="bar"><i class="{cls}" style="width:{width:.1f}%"></i></div>'


def render_audit_html(
    audit: dict,
    findings: list[dict],
    site: dict,
    *,
    checks_meta: dict | None = None,
) -> str:
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
    stamp_cls = "" if scores.get("overall_grade") else " inconclusive"
    demand = scores.get("demand_capture")
    pcr = scores.get("page_citation_readiness")
    bap = scores.get("brand_ai_presence")
    gate = audit.get("gate_state") or scores.get("gate_state") or ""
    generated = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    fails = sum(1 for f in findings or [] if isinstance(f, dict) and f.get("status") == "fail")
    warns = sum(1 for f in findings or [] if isinstance(f, dict) and f.get("status") == "warn")

    out: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="robots" content="noindex">',
        f"<title>AI Demand Capture audit — {_esc(domain)}</title>",
        f"<style>{_CSS}</style>",
        "</head><body><main>",
        # -- masthead ---------------------------------------------------------
        '<header class="masthead">',
        "<div>",
        '<p class="eyebrow">AI Demand Capture &middot; Page Autopsy</p>',
        f"<h1>{_esc(domain)}</h1>",
        f'<div class="meta">{_esc(url)}</div>',
        f'<div class="meta">Audited {_esc(audited_on)}'
        + (f" &middot; gate: {_esc(gate)}" if gate else "")
        + f" &middot; {fails} failing &middot; {warns} warnings</div>",
        "</div>",
        f'<div class="stamp{stamp_cls}"><div class="g">{_esc(grade)}</div>'
        f'<div class="s">{_score_cell(demand)}/100</div>'
        '<div class="l">Demand capture</div></div>',
        "</header>",
        # -- stat row ---------------------------------------------------------
        '<div class="statrow">',
        '<div class="stat"><div class="label">Citation readiness</div>'
        f'<div class="value">{_score_cell(pcr)}</div></div>',
        '<div class="stat"><div class="label">Brand AI presence</div>'
        f'<div class="value">{_score_cell(bap)}</div></div>',
        '<div class="stat"><div class="label">Checks failing</div>'
        f'<div class="value">{fails}</div></div>',
        "</div>",
    ]

    # -- fix queue (the lead section) ------------------------------------------
    queue = _fix_queue(findings, checks_meta)
    if queue:
        out.append("<section><h2>Do these first</h2>")
        out.append(
            '<p class="sub">Highest-impact failures, ordered by severity and check weight.</p>'
        )
        out.append('<ol class="queue">')
        for item in queue:
            f, meta = item["finding"], item["meta"]
            status = str(f.get("status") or "")
            status_cls = status if status in _STATUS_ORDER else "unknown"
            name = meta.get("name") or f.get("check_id")
            ft_label = _FIX_TYPE_LABELS.get(str(f.get("fix_type")), f.get("fix_type") or "")
            evidence = f.get("evidence")
            note = evidence.get("note") if isinstance(evidence, dict) else evidence
            out.append(
                "<li><div>"
                f'<span class="q-name">{_esc(name)}</span> '
                f"<code>{_esc(f.get('check_id'))}</code>"
                + (f'<div class="q-detail">{_esc(note)}</div>' if note else "")
                + "</div>"
                f'<span class="q-tags"><span class="pill p-{status_cls}">{_esc(status)}</span>'
                + (f'<span class="chip">{_esc(ft_label)}</span>' if ft_label else "")
                + "</span></li>"
            )
        out.append("</ol></section>")

    # -- category ledger --------------------------------------------------------
    out.append("<section><h2>Category scores</h2>")
    out.append('<p class="sub">Deterministically recomputed — same inputs, same score.</p>')
    out.append('<div class="ledger">')
    for key in SECTION_KEYS.values():
        counts = _dict_or_empty(section_counts.get(key))
        out.append(
            '<div class="lrow">'
            f'<span class="cat">{_esc(_SECTION_LABELS.get(key, key))}</span>'
            f"{_bar_html(section_scores.get(key))}"
            f'<span class="num">{_score_cell(section_scores.get(key))}</span>'
            "</div>"
        )
        del counts  # counts shown in findings; keep rows compact
    out.append("</div></section>")

    # -- findings grouped by fix_type -------------------------------------------
    out.append("<section><h2>Findings</h2>")
    out.append(
        '<p class="sub">Grouped by who fixes it — shippable today first. '
        "Every finding carries its evidence class.</p>"
    )
    grouped = _group_findings(findings)
    if not grouped:
        out.append("<p>No findings recorded for this audit.</p>")
    for fix_type, group in grouped:
        label = _FIX_TYPE_LABELS.get(fix_type, fix_type)
        out.append(f"<h3>{_esc(label)} ({len(group)})</h3>")
        out.extend(_finding_html(f, checks_meta) for f in group)
    out.append("</section>")

    # -- footer ------------------------------------------------------------------
    out.append(
        "<footer><span>"
        f"Registry {_esc(audit.get('registry_version'))}"
        f" &middot; model {_esc(audit.get('model_version'))}"
        f" &middot; generated {_esc(generated)}</span>"
        '<span class="claim">Every score is recomputed deterministically from evidence.</span>'
        "</footer>"
    )
    out.append("</main></body></html>")
    return "".join(out)


def render_group_html(
    audit: dict,
    site: dict,
    *,
    checks_meta: dict | None = None,
) -> str:
    """Render a group-rollup audits row (gate_state='group_rollup') whose scores
    jsonb carries the assembled group dict: locations, rollup, sitewide, fix_queue."""
    audit = _dict_or_empty(audit)
    site = _dict_or_empty(site)
    data = _dict_or_empty(audit.get("scores"))
    rollup = _dict_or_empty(data.get("rollup"))
    locations = data.get("locations") if isinstance(data.get("locations"), list) else []
    sitewide = data.get("sitewide") if isinstance(data.get("sitewide"), list) else []
    fix_queue = data.get("fix_queue") if isinstance(data.get("fix_queue"), list) else []

    domain = site.get("domain_norm") or site.get("domain") or ""
    audited_on = _fmt_date(audit.get("finished_at") or audit.get("created_at"))
    avg = rollup.get("avg_score")
    pages = rollup.get("pages_audited")
    inconclusive = rollup.get("pages_inconclusive") or 0
    generated = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")

    out: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="robots" content="noindex">',
        f"<title>Group autopsy — {_esc(domain)}</title>",
        f"<style>{_CSS}</style>",
        "</head><body><main>",
        '<header class="masthead">',
        "<div>",
        '<p class="eyebrow">AI Demand Capture &middot; Group Autopsy</p>',
        f"<h1>{_esc(domain)}</h1>",
        f'<div class="meta">{_esc(pages)} locations audited'
        + (f" &middot; {_esc(inconclusive)} unreachable" if inconclusive else "")
        + (f" &middot; {_esc(audited_on)}" if audited_on else "")
        + "</div>",
        "</div>",
        f'<div class="stamp"><div class="g">{_score_cell(avg)}</div>'
        '<div class="s">group avg</div>'
        '<div class="l">Demand capture</div></div>',
        "</header>",
    ]

    # -- fix once, every location benefits (the artifact's core value) ----------
    if sitewide:
        out.append("<section><h2>Fix once — every location benefits</h2>")
        out.append(
            '<p class="sub">Failures shared across locations: one template or schema fix '
            "closes the same finding on every affected page.</p>"
        )
        out.append('<ol class="queue">')
        for s in sitewide:
            if not isinstance(s, dict):
                continue
            name = (
                _meta_for(checks_meta, s.get("check_id")).get("name")
                or s.get("name") or s.get("check_id")
            )
            affected = s.get("affected_urls") if isinstance(s.get("affected_urls"), list) else []
            note = s.get("note") or s.get("evidence_note")
            out.append(
                "<li><div>"
                f'<span class="q-name">{_esc(name)}</span> '
                f"<code>{_esc(s.get('check_id'))}</code>"
                + (f'<div class="q-detail">{_esc(note)}</div>' if note else "")
                + f'<div class="q-detail">Affects {len(affected)} location page(s)</div>'
                "</div>"
                f'<span class="q-tags"><span class="pill p-fail">'
                f"{len(affected)}&times;</span></span></li>"
            )
        out.append("</ol></section>")

    # -- per-location ledger ------------------------------------------------------
    out.append("<section><h2>Locations</h2>")
    out.append('<div class="ledger">')
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        label = loc.get("url") or loc.get("audit_id") or ""
        grade = loc.get("grade")
        score = loc.get("score")
        status = str(loc.get("status") or "")
        top = loc.get("top_issues") if isinstance(loc.get("top_issues"), list) else []
        top_txt = ", ".join(
            str(t.get("check_id")) for t in top[:3] if isinstance(t, dict) and t.get("check_id")
        )
        out.append(
            '<div class="lrow">'
            f'<span class="cat">{_esc(label)}</span>'
            f"{_bar_html(score)}"
            f'<span class="num">{_esc(grade) or _esc(status) or "&mdash;"}</span>'
            "</div>"
            + (f'<div class="lrow"><span class="cat"></span>'
               f'<span class="counts" style="text-align:left">worst: {_esc(top_txt)}</span>'
               "<span></span></div>" if top_txt else "")
        )
    out.append("</div></section>")

    # -- full fix queue -----------------------------------------------------------
    if fix_queue:
        out.append("<section><h2>Complete fix queue</h2>")
        out.append('<p class="sub">Sitewide fixes first, then per-location by severity.</p>')
        out.append('<ol class="queue">')
        for q in fix_queue[:20]:
            if not isinstance(q, dict):
                continue
            name = (
                _meta_for(checks_meta, q.get("check_id")).get("name")
                or q.get("name") or q.get("check_id")
            )
            scope = str(q.get("scope") or "")
            n_pages = q.get("pages_affected")
            hint = q.get("effort_hint")
            out.append(
                "<li><div>"
                f'<span class="q-name">{_esc(name)}</span> '
                f"<code>{_esc(q.get('check_id'))}</code>"
                + (f'<div class="q-detail">{_esc(hint)}</div>' if hint else "")
                + "</div>"
                '<span class="q-tags">'
                + (f'<span class="chip">{_esc(scope)}</span>' if scope else "")
                + (f'<span class="pill p-warn">{_esc(n_pages)} page(s)</span>'
                   if n_pages else "")
                + "</span></li>"
            )
        out.append("</ol></section>")

    out.append(
        "<footer><span>"
        f"Registry {_esc(audit.get('registry_version'))}"
        f" &middot; generated {_esc(generated)}</span>"
        '<span class="claim">Every score is recomputed deterministically from evidence.</span>'
        "</footer>"
    )
    out.append("</main></body></html>")
    return "".join(out)
