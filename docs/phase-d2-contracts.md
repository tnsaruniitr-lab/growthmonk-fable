# Phase D2 module contracts — competitor intelligence pack

The "competitor overview" layer on D0's DataForSEO plumbing: monthly competitor profiles,
systematic discovery, SERP-feature share, a "competitive position" receipt section, opt-in
top-100 depth. Schema: **migration 010** (009 is the last landed — verify before writing).
Style/test rules per docs/phase-a-contracts.md; ZERO network in tests (httpx MockTransport
per test_labs.py); DB tests under the DATABASE_URL skip guard; local pg16 per phase-c-wave2
COMMON. **No LLM anywhere in this wave** — every number is deterministic SQL/Python.
Empty-state law: a competitor with no data renders **"no data yet"**, never zeros.
Reuse-before-buy on every paid call; envelope `cost` (dollars→cents) recorded per call,
falling back to labs.py's TASK_COST_CENTS + ROW_COST_CENTS × rows.

## COMMON — schema (migration 010, WP-A owns the file), payloads, cost rules

```sql
create table competitor_profiles (
  id bigint generated always as identity primary key,
  org_id uuid not null references orgs(id), site_id uuid not null references sites(id),
  domain text not null,       -- normalized host; one of sites.competitor_domains
  checked_on date not null,   -- refresh day (monthly cadence)
  total_keywords int, top10_keywords int, -- organic.count / pos_1+pos_2_3+pos_4_10;
                                          -- NULL = provider had nothing (honest absence)
  est_traffic numeric,                    -- organic.etv (bulk_traffic_estimation wins)
  movers jsonb not null default '{}',     -- {"new","up","down","lost"} from is_new/is_up/...
  raw_metrics jsonb not null default '{}', provider text not null default 'dataforseo',
  cost_cents numeric(10,4) not null default 0, fetched_at timestamptz not null default now(),
  unique (site_id, domain, checked_on)    -- same-day re-runs upsert, idempotent
);
-- + RLS block (006's do $$ pattern). Same file: queue_items kind check +=
-- 'competitor_candidate' (mirror 008's drop/re-add); alter table tracked_queries add column
-- serp_depth int not null default 10 check (serp_depth in (10,100));
-- alter table serp_snapshots add column depth int not null default 10;
```

Jobs (leased, idempotent, site-scoped via job.site_id or payload.site_id, org_id resolved
from sites when absent — copy handle_keyword_gap's pattern):
- `refresh_competitor_profiles` payload `{}` / `{"site_id"}` — monthly schedules row
  (every_minutes=43200); no site_id ⇒ every site with non-empty competitor_domains.
- `discover_competitors` payload `{"site_id", "limit": 10}` — operator-triggered, no schedule.

Labs endpoints (POST one-task arrays, Basic auth, 200-envelope, location_code 2784 /
language "en"; fixtures reuse test_labs.py's labs_envelope wrapper):
- `/v3/dataforseo_labs/google/domain_rank_overview/live` `[{target, ...}]` → items
  `[{metrics: {organic: {pos_1, pos_2_3, pos_4_10, pos_11_20, count, etv, is_new, is_up,
  is_down, is_lost}, paid: {...}}}]`
- `/v3/dataforseo_labs/google/bulk_traffic_estimation/live` `[{targets: [d1..dN], ...}]` →
  items `[{target, metrics: {organic: {count, etv}, ...}}]` — ONE call per site.
- `/v3/dataforseo_labs/google/competitors_domain/live` `[{target: client_domain, limit, ...}]`
  → items `[{domain, avg_position, sum_position, intersections, full_domain_metrics:
  {organic: {count, etv}}}]`. NOTE: HANDOFF §D2 says "domain_intersection"; that pairwise
  endpoint compares two KNOWN domains and cannot discover — competitors_domain is the
  intersection-ranked discovery endpoint. Deliberate, documented deviation.

Cost/quota: cost_events purposes `labs_domain_rank_overview` / `labs_bulk_traffic` /
`labs_competitors_domain` (provider 'dataforseo', one event per call). MAX_COMPETITORS = 10 —
refresh/discover refuse larger configs with an honest note, never silently truncate.
Refresh reuse-before-buy: a (site, domain) profile row with fetched_at within max_age_days
(default 25) is a cache hit — a monthly tick buys each domain at most once. Top-100 depth:
provider bills ~double per extra 100 results (depth=100 ≈ 2× the ~$0.002 depth-10 price);
default stays 10, 100 only via explicit `gm track` flags. Snapshot cache honors depth: a
row satisfies a request iff `row.depth >= requested`.

## WP-A — profiles: migration 010 + labs.py appends + gm/intel/competitors.py

Owns: `ops/migrations/010_phase_d2_competitor_intel.sql`, `gm/intel/labs.py` (append-only),
NEW `gm/intel/competitors.py`, NEW `tests/test_competitor_profiles.py`.

labs.py — three LabsClient methods, same envelope/retry/last_cost_cents discipline:
```python
def domain_rank_overview(self, domain, *, location_code=2784, language="en") -> dict | None
    # {"total_keywords","top10_keywords","pos_1","movers":{...},"raw": metrics}; None when the
    # provider returns no items (caller stores a NULLs row — honest absence, no invention)
def bulk_traffic_estimation(self, domains: list[str], *, location_code=2784, language="en") -> dict
    # {domain: {"est_traffic": float|None, "total_keywords": int|None}}; missing targets absent
def competitors_domain(self, domain, *, location_code=2784, language="en", limit=30) -> list[dict]
    # [{"domain","intersections","avg_position","their_keywords","their_etv"}] normalized hosts
```
competitors.py:
```python
def refresh_competitor_profiles(conn, *, org_id, site_id, labs_client=None, max_age_days=25) -> dict
    # per sites.competitor_domains: cache-check → domain_rank_overview; ONE bulk_traffic_
    # estimation for the misses; upsert competitor_profiles checked_on=today (same-day
    # idempotent); record_cost per call. Returns {"competitors","refreshed","cached","empty",
    # "cost_cents","note"} — note set (zero spend) on empty or >MAX_COMPETITORS configs.
def handle_refresh_competitor_profiles(ctx)   # job wiring per COMMON
def latest_profile(conn, site_id, domain) -> dict | None
    # newest row {"domain","total_keywords","top10_keywords","est_traffic","movers",
    # "checked_on"}; None when never fetched — callers render "no data yet"
```
Tests: fixtures for all three endpoints (success, empty-items, 40501 non-retryable, 429→retry
with `_sleep` patched), cost fallback formula, refresh with a fake labs client (call counts
prove reuse-before-buy + the single bulk call), same-day idempotence, empty/oversize notes,
latest_profile None. DB parts under the skip guard.

## WP-B — discovery: gm/intel/discovery.py

Owns: NEW `gm/intel/discovery.py`, NEW `tests/test_competitor_discovery.py`.

```python
def discover_competitors(conn, *, org_id, site_id, labs_client=None, limit=10) -> dict
    # competitors_domain(client domain_norm) → filter out: the client (subdomain-aware),
    # domains already in sites.competitor_domains, gm.audit.compare.DOMAIN_DENYLIST
    # (read-only import), intersections < 3; keep top `limit` by intersections. Upsert
    # queue_items kind='competitor_candidate' via detectors._upsert_item: target=
    # {"domain": host}, at_stake={"intersections","avg_position","their_keywords",
    # "their_etv","basis":"labs"} — open-refresh/dismissed-snooze/actioned-untouched
    # discipline comes free. Returns {"candidates","queued","cost_cents","note"}.
def handle_discover_competitors(ctx)          # job wiring per COMMON
def confirm_candidate(conn, *, site_id, domain) -> bool
    # append to sites.competitor_domains (normalized, deduped) + queue item → 'actioned';
    # missing candidate row still appends (gm site set-competitors hand-picking stays legal)
def dismiss_candidate(conn, *, site_id, domain, snooze_days=90) -> bool
    # queue item → 'dismissed', snooze_until = now() + snooze_days
```
Tests: filter matrix (self/subdomain, configured, denylist, low intersections), limit +
ordering, queue discipline incl. dismissed-snooze re-runs, confirm append/dedupe/actioned,
dismiss snooze, cost recording. Fake labs client throughout; DB skip guard.

## WP-C — feature share + competitive position + receipt section

Owns: NEW `gm/intel/feature_share.py`, `gm/intel/serp.py` (append-only),
`gm/delivery/receipts.py` (this wave's sole owner), NEW `tests/test_d2_competitive.py`.

serp.py appends: (1) `_normalize_items` retains feature OWNERS going forward —
featured_snippet feature gains `"domain"`/`"url"`; people_also_ask gains `"source_domains"`
(normalized answer-source hosts, encounter order, deduped); ai_overview already carries
cited_domains. Old snapshots lack the fields — feature_share counts them "unattributed",
never guesses. (2) `get_snapshot(..., depth: int = 10)` — cache hit requires
`row.depth >= depth`; purchases pass depth to serp_live and store the column.

feature_share.py (pure assembly over serp_snapshots — ZERO new spend):
```python
def feature_share(conn, site_id, *, since: date, until: date) -> dict
    # panel = active tracked_queries; Mon-start weekly buckets over in-window snapshots; per
    # feature in (ai_overview, featured_snippet, people_also_ask) per week:
    # {"present": n_queries, "you": n, "competitors": {host: n}, "other": n, "unattributed": n}
    # (attribution subdomain-aware vs domain_norm / competitor_domains). Returns
    # {"weeks": [{"week_start","features":{...}}], "queries": n, "note": str|None} — note when
    # the panel is empty or every snapshot predates owner retention.
def competitive_position(conn, site_id, *, since: date, until: date) -> dict
    # THE section data contract (renderer consumes exactly this):
    # {"window": {"since","until"},
    #  "you": {"domain","tracked_queries","rank_top3","rank_top10","aio_citations",
    #          "audit_median": float|None, "audit_n"},
    #  "competitors": [per sites.competitor_domains, configured order:
    #    {"domain","rank_top3","rank_top10",    # last-in-window rank_history row per query —
    #     "aio_citations",                      #   host position in top_domains fingerprint;
    #                                           #   host ∈ aio_cited_domains, subdomain-aware
    #     "audit_median": float|None,"audit_n", # statistics.median of scores->overall_score,
    #                                           #   done in-window audits w/ gate_state=
    #                                           #   'competitor_reference', url host matching
    #     "profile": latest_profile(...)|None,  # lazy import gm.intel.competitors, tolerate
    #                                           #   absence (D0 _rank_movement_fn pattern)
    #     "has_data": bool}],                   # False ⇒ "no data yet"; zeros FORBIDDEN
    #  "feature_share": feature_share(...)}
    # "you" audit stats mirror assemble_site_receipt's page-audit filter (draft_id null,
    # excluded gate states). No tracked queries / no competitors ⇒ honest notes, empty lists.
```
receipts.py: assemble_site_receipt gains `payload["competitive_position"]` via a lazy
`_competitive_position_fn()` accessor (window = rank_tracking's since/until);
render_receipt_html renders `_competitive_html(payload)` — "Competitive position" section
directly after 'Google visibility': you-row first, one row per competitor (rank counts, AIO
citations, audit median n=, profile keywords/traffic/movers), then the feature-share weekly
mini-table; every value through report._esc; honest empty states for section-absent /
no-competitors / has_data=False.
Tests: owner retention (featured_snippet w/ + w/o domain, PAA sources, legacy rows), depth
cache rule (100 serves 10; 10 never serves 100), weekly buckets + unattributed, median math +
competitor_reference gating, has_data law, receipt golden assertions incl. every empty state
+ hostile-string escaping. DB skip guard; run the full suite, report counts.

## WP-D — CLI, worker, depth plumbing, admin surface

Owns: `gm/cli.py` (this wave's sole owner), `gm/intel/rank_tracker.py`, `gm/api.py`,
NEW `tests/test_d2_cli.py`.

New `competitors` sub-app (existing Typer patterns; imports WP-A/B/C functions, never
reimplements):
```
gm competitors list <domain>            # configured + latest_profile line each ("no data
                                        # yet" when None) + open candidate count
gm competitors discover <domain> [--limit 10] [--now]   # enqueue/run discover_competitors
gm competitors confirm <domain> <host>                  # candidate → sites.competitor_domains
gm competitors dismiss <domain> <host> [--snooze-days 90]
gm competitors refresh <domain> [--now] [--monthly]     # --monthly inserts the schedules row
                                        # (refresh_competitor_profiles, every_minutes=43200)
gm competitors position <domain> [--month YYYY-MM]      # competitive_position + feature_share
                                        # as text (empty states rendered verbatim)
gm track add <domain> <query> [--target-page ...] [--depth 10|100]   # depth → serp_depth
gm track set-depth <domain> <query> --depth 10|100
```
worker(): register `refresh_competitor_profiles` + `discover_competitors` (lazy-import
wrappers à la _lazy_send_lead_card). rank_tracker.py: track_site selects serp_depth per query
and passes `depth=` to serp.get_snapshot — find_rank/top_domains unchanged (a #47 rank
records honestly; top_domains stays a top-10 fingerprint). api.py: authed
`GET /admin/sites/{id}/competitors` returning current-month competitive_position JSON (mirror
the /admin/sites/{id}/leads guards).
Tests: track --depth writes serp_depth + track_site passes depth (fake serp client asserts),
confirm/dismiss round-trip, --monthly schedules row shape, admin route via TestClient (auth
guard + empty-state body), worker handler registration. DB skip guard.

## Integrator notes
No WP touches another's files; WP-B/C/D consume WP-A/C additions strictly by the signatures
above. D3 item 8 (default schedules on `gm site add`) will fold refresh in — do not wire
onboarding here. e2e against prod before done: one real refresh (≤$0.15 for 3 competitors),
one discovery run, one receipt render with the new section — evidence under ops/evidence/.
