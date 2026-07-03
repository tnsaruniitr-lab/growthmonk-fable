# Phase B wave-1 module contracts

Binding interfaces for the parallel port of the auditor IP from
`/Users/arunsharma/Documents/New project/aeo-seo-auditor-fable` (read-only source — never modify it;
its predecessor `/Users/arunsharma/Documents/New project/aeo-seo-auditor` may hold extra fixtures in
`scripts-v2/`, and the ruleset docs may also live under `/Users/arunsharma/Documents/New project/.claude/skills/`
or the fable repo's `service/references/` — search all three). Target schema:
`ops/migrations/002_phase_b_audits.sql`. Shared plumbing from Phase A (`gm.config`, `gm.db`,
Phase A contracts) is unchanged. Style/test rules are identical to docs/phase-a-contracts.md:
Python 3.12, ruff line 100 (verify with `.venv/bin/ruff check platform`), pytest with
DB-dependent tests skipped when DATABASE_URL is unset, NO network calls in tests.

## Registry data format (registry/checks/*.json)

One file per category letter (a.json … j.json), each a JSON array of check objects:

```json
{
  "check_id": "A-01",
  "check_version": 1,
  "category": "A",
  "category_name": "Technical SEO",
  "name": "…",
  "description": "what is being verified and why it matters (1-3 sentences)",
  "applies_to": ["all"],
  "method": "deterministic | llm | measured | comparative",
  "badge": "hard_evidence | measured | static_rule | comparative | heuristic | model_judgment",
  "criteria": {"pass": "…", "warn": "…", "fail": "…"},
  "weight": 2,
  "severity": "critical | high | medium | low",
  "fix_type": "page_html | schema | content_restructure | sitewide_template | cms_constraint | offpage_entity | cannot_fix_from_page",
  "fix_template": "…",
  "sources": ["…"]
}
```

Rules: transcribe faithfully from the source ruleset (v1.3, 103 checks; the "97" in some filenames
is stale) — do not invent checks, do not drop checks, preserve thresholds and research citations
verbatim where present. `check_version` is 1 for every check in this extraction. Every check MUST
carry a badge and fix_type; when the source doesn't state one, infer conservatively and add
`"inferred": ["badge"]`. Keep category letters/counts consistent with the source
(A:12 B:11 C:13 D:13 E:13 F:12 G:9 H:8 I:8 J:4 — verify against what you actually find and
report any drift in notes rather than forcing the counts).

## gm/audit/fetch.py + safety.py + bev.py (fetch & bot's-eye-view)

```python
# safety.py — port fable service/safety.py faithfully:
def validate_url(url: str) -> str            # raises UnsafeURL; blocks non-http(s), creds,
                                             # private/loopback/link-local/metadata IPs (incl. IPv4-mapped IPv6)
# fetch.py
@dataclass
class FetchResult: url: str; final_url: str; status_code: int; headers: dict[str, str]
                   text: str; elapsed_ms: int; redirect_chain: list[str]
Fetcher = Callable[[str], FetchResult]
def make_fetcher(client: httpx.Client | None = None, user_agent: str | None = None) -> Fetcher
    # SSRF-validates the URL AND every redirect hop; 30s timeout; max 5 redirects;
    # injectable client for tests (httpx.MockTransport)
# bev.py — port bots_eye_view.sh + _bev_analyze.py logic to pure Python:
@dataclass
class BevResult: classification: str   # fully_accessible|partial_ssr|js_dependent|minimal_content|spa_no_ssr
                 per_ua: dict[str, dict]  # ua -> {status, bytes, title, h1, blocked}
                 cloaking_suspected: bool; spa_shell: bool; notes: list[str]
def bots_eye_view(url: str, fetcher_factory: Callable[[str], Fetcher]) -> BevResult
    # UA set (keep fable's): default browser, Googlebot, GPTBot, PerplexityBot, ClaudeBot + a 404 probe
    # keep fable's byte-comparison cloaking heuristic and 404-similarity SPA-shell detection thresholds
```

## gm/audit/inspectors/ (robots, sitemap, schema_markup)

Port fable's `service/scripts/check_robots_txt.py`, `check_sitemap.py`, and the schema
completeness validator (28 @type field registries) into importable modules (no subprocess, no bash):

```python
def inspect_robots(robots_txt: str | None, target_url: str) -> dict     # per-bot allow/deny w/ fable's
                                                                        # UA-precedence + longest-path rules (16 bots)
def inspect_sitemap(fetch: Fetcher, base_url: str, robots_txt: str | None) -> dict
    # discovery + recursion + deterministic MD5-seeded sampling (keep fable's algorithm + 10-URL sample)
def inspect_schema(html: str, page_url: str) -> dict                    # JSON-LD extraction, @type registries,
                                                                        # required/recommended field completeness
```
Return dicts must be JSON-serializable and preserve the source scripts' output keys where feasible
(they feed check evidence). Port the source repos' fixtures (XML entities/CDATA sitemap cases,
FAQ true/false-positive pages, etc.) into `platform/tests/fixtures/` and adapt their regression
tests. Tests take strings/fake fetchers — no network.

## gm/audit/scoring.py + delta.py + registry.py

```python
# registry.py
@dataclass
class Registry: version: str; checks: dict[str, dict]        # check_id -> check object
def load_registry(root: Path | None = None) -> Registry
    # reads registry/manifest.json + registry/checks/*.json; validates: unique check_ids,
    # every check has badge/fix_type/weight, category letters match filenames; raises on violation
# scoring.py — port fable service/scoring.py:
def recompute_scores(findings: list[dict], registry: Registry, gate_state: str) -> dict
    # deterministic: per-section weighted points w/ N/A renormalization, PCR composite,
    # 9-grade table, BAP kept separate (never folded into the letter grade),
    # demand-capture headline score; transport-inconclusive gate refuses to grade;
    # clamps [0,100]; enum-guards statuses; pure function of inputs (assert weight sums)
def validate_findings(findings: list[dict], registry: Registry) -> list[dict]
    # neutralizes non-numeric/forged values, unknown check_ids -> dropped with note
# delta.py — port fable delta.py onto the audits/audit_findings schema:
def audit_delta(before: list[dict], after: list[dict]) -> dict
    # findings keyed by check_id -> resolved/regressed/new_issues/persisting + score movement;
    # comparability rule: a check_id is comparable iff check_version matches in both (ADR-13);
    # non-comparable checks reported separately, never counted as resolved/regressed
```
Port fable's scoring tests (determinism, forged-value neutralization, clamps) and add delta
comparability tests. Section weights: keep fable's values; if fable's section keys differ from
registry category letters, map explicitly in one place with a comment. Tests use a small inline
sample registry (do NOT depend on the real registry/ files being present — they are extracted
concurrently by other agents).
