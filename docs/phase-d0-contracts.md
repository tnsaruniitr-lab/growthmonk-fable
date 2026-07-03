# Phase D0 module contracts — SERP tracking & keyword gap

Threads DataForSEO through the remaining loop stages: rank + AI-Overview tracking (verify/receipt),
keyword-gap detection (queue), SERP context in audits (autopsy). Schema: migration 006.
Style/test rules per docs/phase-a-contracts.md; local pg16 recipe per phase-c-wave2 COMMON.
ZERO network in tests. Reuse-before-buy discipline everywhere (serp.get_snapshot).

## Agent A — gm/intel/rank_tracker.py (+ append-only additions to gm/intel/serp.py)

serp.py append rights (this wave, agent A only): `extract_ai_overview(raw_response) -> dict`
{"present": bool, "cited_domains": [normalized hosts]} parsed from the ai_overview item of a
serp_live raw response (defensive: absent item -> present=False; references/links arrays vary —
collect every url/domain field found under the item; normalize via engines.base.normalize_host).
Also extend serp_live's SerpResult normalization ONLY IF needed to retain the raw ai_overview item.

rank_tracker.py:
```python
def add_tracked_query(conn, org_id, site_id, query, target_page=None) -> str
def track_site(conn, *, org_id, site_id, serp_client=None, max_age_days=6) -> dict
    # for each active tracked_query: get_snapshot (max_age_days=6 so a weekly schedule buys
    # at most one snapshot per query) -> compute: client rank + ranked_url (subdomain-aware
    # via normalize_host vs sites.domain_norm), aio fields via extract_ai_overview on the
    # snapshot's stored raw (serp_snapshots.results lacks raw — store aio fields at snapshot
    # time? NO: get_snapshot returns the row incl. features; extend features to carry the
    # ai_overview parse when agent A adds it to serp_live) -> upsert rank_history for
    # checked_on = today (idempotent re-runs same day). Returns counts + spend.
def handle_track_serps(ctx)   # job 'track_serps' (weekly via schedules); payload {}
def rank_movement(conn, site_id, *, since: date, until: date) -> list[dict]
    # per query: first/last rank + aio_cited in window, competitor top_domains changes
    # (entered/left top-10) — pure assembly for receipts
```
Tests: aio extraction fixtures (present w/ citations incl. client, absent, malformed),
rank detection (subdomain, absent -> NULL not 0), same-day idempotence, movement assembly
(entered/left top-10 diffs), DB skip guard.

## Agent B — gm/intel/labs.py (keyword gap via DataForSEO Labs)

```python
class LabsClient:   # same auth/retry/envelope pattern as serp.DataForSeoClient (local copy or
                    # import the shared post pattern from serp if exported; do NOT modify serp.py)
    def ranked_keywords(self, domain, *, location_code=2784, language="en", limit=200,
                        position_max=20) -> list[dict]
        # /v3/dataforseo_labs/google/ranked_keywords/live: [{query_norm, position, volume, cpc, url}]
def keyword_gap(conn, *, org_id, site_id, labs_client=None, volume_floor=10,
                position_max=10, per_competitor_limit=200) -> dict
    # competitors = sites.competitor_domains (skip w/ honest note when empty);
    # gap = queries where >=1 competitor ranks <= position_max AND the query is absent from
    # BOTH the client's rank_history (any rank) and gsc_window_agg/gsc_daily 28d (any impressions)
    # AND volume >= volume_floor; dedupe across competitors keeping best (position, volume);
    # upsert queue_items kind='keyword_gap' (target={"query": q}, at_stake={"volume": v,
    # "best_competitor": d, "their_position": p, "basis": "labs"}); respects the standard
    # queue upsert/snooze discipline (reuse detectors._upsert_queue_item if importable — read
    # detectors.py; else mirror it exactly); records cost_events (Labs $0.01/task + $0.0001/row).
def handle_keyword_gap(ctx)   # job 'keyword_gap'; payload {}
```
Tests: Labs envelope/normalization fixtures, gap filtering (client-present exclusion via both
sources, volume floor, dedupe best-of), queue upsert discipline incl. dismissed-snooze, cost
recording, DB skip guard.

## Agent C — integration surfaces (owns this wave's edits to pipeline.py, receipts.py, report.py)

1. pipeline.py: run_page_audit gains keyword-only serp_context=None — when provided
   ({"query": ..., "results": [...], "client_rank": ..., "features": [...]}), it is added to the
   evidence bundle as evidence["serp"] AND comparative_na_overrides treats the bundle as having
   comparison data (H checks classify instead of na). Build the context in a new helper
   `serp_context_for_page(conn, site_id, url_norm)` reading the freshest rank_history+serp_snapshots
   row whose target_page/ranked_url matches the page (None when absent — behavior unchanged).
   handle_audit_page wires it automatically. Public signatures otherwise unchanged.
2. report.py: masthead meta line gains rank context when audit.scores carries serp_context
   summary — pipeline should persist {"query", "client_rank"} into scores["serp_context"] when
   used; render "ranks #7 for 'query'" (escaped) in the masthead meta. Keep escape discipline.
3. receipts.py: assemble_site_receipt payload gains "rank_tracking" section via
   rank_tracker.rank_movement (lazy import, tolerate absence) — per-query first/last rank,
   aio_cited transitions, competitors entered/left top-10; render_receipt_html renders it as a
   'Google visibility' section (rank arrows, AIO cited badges, competitor-change lines), before
   the BETA citation section. Honest empty state when no tracked queries.
Tests: serp_context evidence + H-activation + scores round-trip (extend test_draft_audit-style
fixtures in a NEW test file you own: test_d0_integration.py), masthead rank line escaping,
receipt section golden assertions + empty state. Run the full existing suite; report counts.
