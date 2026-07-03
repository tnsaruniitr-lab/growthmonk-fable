# Phase C wave-2 module contracts

The competitive-intelligence layer + brief generator. Answers two customer questions precisely:
"what can we do NOW to be more visible" (queue + fixes + briefs) and "what are my competitors
doing better" (comparative audits + keyword gap). Schema: `ops/migrations/004_phase_c_content_loop.sql`.
Style/test rules per docs/phase-a-contracts.md. ZERO network in tests (httpx.MockTransport /
recorded fixtures). Env: DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD (Basic auth).

## gm/intel/serp.py  (SerpDataPort — DataForSEO)

```python
class SerpError(Exception): retryable: bool
class DataForSeoClient:
    def __init__(self, login=None, password=None, client: httpx.Client | None = None)
    def serp_live(self, query: str, *, location="United Arab Emirates",
                  language="en", depth=10) -> SerpResult
        # POST /v3/serp/google/organic/live/regular (cheapest live: ~$0.002/req at depth<=10).
        # Normalize: organic -> [{rank, url, domain, title, description, type}];
        # features -> list of item types present (people_also_ask entries include their
        # question strings); keep raw response for the snapshot row.
    def search_volume(self, queries: list[str], *, location_code=2784, language="en") -> dict
        # POST /v3/keywords_data/google_ads/search_volume/live — {query_norm: {volume, cpc,
        # competition}}; tolerate nulls (low-volume terms come back None).
# Retry/backoff per the engines pattern (local copy); DataForSEO wraps errors in a 200 envelope —
# check tasks[0].status_code (20000 = ok; 40xxx = client error -> non-retryable SerpError).

def get_snapshot(conn, site_id, query, *, max_age_days=7, client=None,
                 location=...) -> dict     # reuse-before-buy: latest serp_snapshots row within
                                           # TTL, else pull via client, insert row + cost_event,
                                           # return {id, results, features, fetched_at, fresh}
def get_volumes(conn, site_id, queries, *, max_age_days=30, client=None) -> dict
                                           # same discipline against keyword_metrics
query_norm = lambda q: " ".join(q.lower().split())
```

## gm/audit/compare.py  (comparative audit — "what are my comps doing better, precisely")

```python
def pick_competitors(results: list[dict], client_domain: str, *, limit=3) -> list[dict]
    # top organic entries above (or absent) the client, excluding the client's own domain,
    # subdomain-aware (normalize_host), excluding non-auditable types (maps, video) and
    # mega-platforms via a small DOMAIN_DENYLIST (instagram/facebook/youtube/tripadvisor/
    # booking/yelp/linkedin/wikipedia/reddit...) — a med-spa can't 'out-page' Instagram.
def run_comparison(conn, *, org_id, site_id, query: str, llm, client_page_url=None,
                   registry=None, fetcher_factory=None, serp_client=None,
                   cost_cap_cents_per_page=150.0) -> str   # returns serp_comparisons.id
    # 1. snapshot = serp.get_snapshot (records client's own rank when present)
    # 2. competitors = pick_competitors(...)  (2-3)
    # 3. client audit: latest done audit for the client page if < 14 days old, else run one
    # 4. run_page_audit each competitor URL (their own sites rows are NOT created — audits
    #    row gets site_id of the CLIENT site, url = competitor url, and scores are stored but
    #    the audit is tagged gate_state='competitor_reference' so it never pollutes the
    #    client's own history/deltas)
    # 5. gaps = checks where client is fail/warn AND >= half the audited competitors pass —
    #    [{check_id, name, client_status, competitors_passing, competitor_urls}] ordered by
    #    severity*weight; summary = {client_rank, competitor_ranks, avg_scores}
    # 6. persist serp_comparisons row; return id
def handle_compare_serp(ctx)   # job 'compare_serp' payload {query, page?}
```

## gm/content/briefs.py  (brief generator v1)

```python
def generate_brief(conn, *, org_id, site_id, query: str, llm, kind="new",
                   page_url=None, queue_item_id=None, serp_client=None,
                   registry=None, cost_cap_cents=60.0) -> str    # briefs.id
    # Assemble deterministically FIRST (no LLM):
    #   snapshot (get_snapshot) -> top-10 table + PAA questions
    #   volumes (get_volumes) for the query + PAA-derived terms
    #   comparison (latest serp_comparisons for query if < 14d, else run_comparison when llm
    #     budget allows, else proceed without — brief notes the absence honestly)
    #   client audit findings (fail/warn on the target page) -> required-fix list w/ citations
    #   brand: sites.brand_terms + sites.notes (brandsmith profile lands later — leave a
    #     brand_profile hook reading a 'brand_profiles' row IF a table exists, tolerate absence)
    # THEN one LLM call (json_only, CallBudget) to synthesize: angle (the row nobody covers),
    # outline (answer-first, question H2s incl. PAA), title/meta suggestions. LLM output is
    # ADVISORY — the deterministic sections persist even when the call fails (parse failure ->
    # brief.status stays 'draft' with synthesis=null + note).
    # Persist briefs row: target, brief jsonb {serp_table, paa, volumes, gaps, required_fixes,
    # synthesis}, serp_snapshot_ids, comparison_id, source_audit_id, cost.
def render_brief_markdown(brief_row: dict, checks_meta: dict | None = None) -> str
    # Operator/client-forwardable markdown: target + volume, the competitor coverage table,
    # PAA to answer, required fixes (check names + citations), outline. Pure function.
def handle_generate_brief(ctx)  # job 'generate_brief' payload {query, kind, page?, queue_item_id?}
```

## Integrator wires (not agent-owned)

CLI: gm serp <domain> <query> · gm compare <domain> --query · gm brief <domain> --query [--now]
· worker registers compare_serp + generate_brief. Report/queue surfacing comes with C3.

## Test requirements

serp: envelope parsing (20000 ok / 40501 error fixture), normalization (organic + PAA
extraction), retry-then-success, snapshot reuse-before-buy TTL logic (DB skip guard), volume
null tolerance. compare: pick_competitors (client exclusion incl. subdomains, denylist, above-
client selection, absent-client case), gap math (>= half competitors passing), competitor
audits tagged competitor_reference (DB e2e w/ FakeLlm + fake fetchers + fake serp client).
briefs: deterministic assembly without LLM (synthesis=null path), PAA/volume merge, required-fix
extraction, markdown renderer golden assertions, one DB e2e with all fakes.
