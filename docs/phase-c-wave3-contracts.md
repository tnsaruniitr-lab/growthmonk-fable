# Phase C wave-3 module contracts

The fulfillment close: fix-closer (convergence fix applied), draft grading against OUR registry,
WordPress publish + verify, Delta Receipt v1. Schema: migrations 004 + 005. Style/test rules per
docs/phase-a-contracts.md; local pg16 recipe in docs/phase-c-wave2-contracts.md COMMON block.
READ docs/convergence-diagnosis.md before touching anything content-engine related.

## gm/content/engine_port.py + fixcloser.py  (agent A)

The content engine is the EXISTING serp-analyzer Express service (sibling repo), reached via
CONTENT_ENGINE_URL (+ CONTENT_ENGINE_TOKEN bearer if set). Read its request/response shapes from
'/Users/arunsharma/Documents/New project/serp-analyzer/src/blog/types.ts' and the routes in
src/index.ts / src/blog/*.ts (READ-ONLY — no changes to that repo). Convergence fix is REQUEST-SIDE
(diagnosis doc): enforce_human_signals=true, real author entity w/ sameAs from sites.author,
first-party data from sites.first_party — never fabricate; when sites.author is empty the job
FAILS FAST with a clear "set author first: gm site set-author" error (an ungrounded draft is the
bug we just diagnosed, not a degraded mode).

```python
class EngineUnavailable(Exception): ...
class ContentEngine:
    def __init__(self, base_url=None, token=None, client: httpx.Client | None = None)
    def write_and_audit(self, request: dict, *, timeout=900) -> dict   # their package + audit
def build_writer_request(site: dict, brief_row: dict, *, kind: str) -> dict
    # brief jsonb -> their BlogWriterRequest: topic/keywords/intent from brief target+synthesis,
    # sources from brief serp table (primary-tier competitor URLs), PAA -> questions,
    # author entity + first_party from sites columns, enforce_human_signals=True, brand from
    # sites.brand_terms/notes. Pure function, unit-tested against a recorded types.ts shape.
def handle_close_fixes(ctx)   # job 'close_fixes' payload {content_item_id}
    # brief -> build request -> engine.write_and_audit -> drafts row (version=next, package,
    # cost estimated from their response if present) -> run_draft_audit (agent B's function,
    # import lazily) -> drafts.scorecard_audit_id + human_todos (their audit's open items +
    # our failing checks) -> content_items.status='review'. Engine down/missing env ->
    # EngineUnavailable -> job fails with the honest error (retryable by re-enqueue).
```

## pipeline draft-audit variant + comparative-N/A  (agent B — owns pipeline.py edits this wave)

```python
def run_draft_audit(conn, *, org_id, site_id, draft_html: str, url_hint: str, llm,
                    registry=None, cost_cap_cents=150.0, draft_id=None) -> str
    # audits row with draft_id set, gate_state='draft', url=url_hint. No fetch/BEV/robots/
    # sitemap: transport/crawl-dependent checks (A robots/sitemap ids, BEV-based E ids,
    # B measured ids — derive the set from registry method/badge + a small explicit id list,
    # documented) are marked 'na' with note 'not applicable pre-publish'. inspect_schema runs
    # on the provided HTML; classification runs with the draft evidence bundle; deterministic
    # grading as usual. Findings persisted (citations attached as normal).
ALSO the comparative-N/A optimization in classify/validate: checks with method='comparative'
become status 'na' (note 'requires comparison data') instead of 'inconclusive' when the evidence
bundle carries no comparison section — in BOTH run_page_audit and run_draft_audit. Update the
affected pipeline tests; add draft-audit tests (na-set correctness, schema inspector on HTML,
grades computed, draft_id round-trip).
```

## gm/delivery/wordpress.py + verify.py (+ gsc inspect)  (agent C)

```python
# wordpress.py — Application Passwords, least-privilege (security review):
class WpError(Exception): retryable
class WpClient:
    def __init__(self, base_url, username, app_password, client=None)  # https only — refuse http
    def me(self) -> dict                      # /wp/v2/users/me?context=edit incl. capabilities
    def preflight(self) -> dict               # auth ok, role warning if administrator,
                                              # create+delete a private test draft, report
                                              # {ok, role, warnings[], errors[]}
    def publish_draft(self, *, title, content_html, excerpt=None, meta_jsonld=None) -> dict
        # POST /wp/v2/posts status=draft; JSON-LD injected as a <script type="application/ld+json">
        # block prepended to content (kses caveat noted in result when stripped — compare
        # round-trip); returns {id, link, status}
def connect_wordpress(conn, *, org_id, site_id, base_url, username, app_password) -> dict
    # preflight, then vault store_connection(kind='wordpress'); returns preflight report
def handle_publish(ctx)      # job 'publish' payload {content_item_id, draft_id}
    # WP draft-mode publish -> publish_events row + pages upsert (canonicalize) + content_item
    # page_id/status='published' + IndexNow ping (fire-and-forget, key from env INDEXNOW_KEY,
    # skipped w/ note when unset) + enqueue verify_publish T+15m and T+72h
# verify.py:
def handle_verify_publish(ctx)  # payload {content_item_id, attempt: 'early'|'late'}
    # BEV re-probe of publish url (all UAs read content?), schema present in served HTML,
    # GSC URL inspection when a gsc connection exists (add inspect_url(url) to
    # gm/connections/gsc.py — you MAY append that one method + its test) ->
    # verify_events row; late attempt verdict: pass -> status='verified',
    # fail -> 'verify_failed' + honest result
```

## gm/delivery/receipts.py  (agent D)

```python
def compute_content_delta(conn, *, content_item_id) -> str   # content_deltas row id
    # windows pivot on publish_events.published_at (28d each side, GSC-lag offset 3d);
    # gsc windows from gsc_daily FINAL days only (page url_norm join incl. page_url_history);
    # missing GSC data -> honest empty {} sections, never zeros; findings_diff via
    # gm.audit.delta.audit_delta(before=pre-publish page audit findings, after=latest
    # post-publish audit) with the ADR-13 comparability rule; before_audit = latest done
    # page audit before publish, after_audit = latest after (skip when either absent, note it)
def assemble_site_receipt(conn, *, site_id, period: str) -> str   # site_deltas row id
    # rollup for the month: audits run, score movements, fix log (levers + content_items),
    # citation rates ± Wilson CI per prompt vs prior period + control-site drift (reuse
    # gm.intel.variance), queue actions taken, spend (cost_events sum)
def render_receipt_html(site: dict, payload: dict, *, checks_meta=None) -> str
    # the monthly Delta Receipt on the report design system (reuse _CSS/_esc/badge patterns by
    # IMPORTING from gm.delivery.report — do not fork the stylesheet; add only receipt-specific
    # section markup here): masthead + stamp (score movement), fix log, findings resolved/
    # regressed (comparable-only, per ADR-13), GSC before/after table or honest 'no GSC
    # connection' line, citation section labeled BETA w/ CIs, claim-ceiling footer line
def handle_compute_delta(ctx); def handle_assemble_receipt(ctx)   # job types
```

## Integrator wires (not agent-owned)
CLI: gm site set-author / gm close-fixes / gm wp connect / gm publish / gm receipt;
worker registers close_fixes, publish, verify_publish, compute_delta, assemble_receipt.
