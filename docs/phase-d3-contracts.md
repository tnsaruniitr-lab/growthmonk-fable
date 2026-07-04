# Phase D3 module contracts — Phase D tail (schedules · local presence · AdsPort)

HANDOFF §2 items 6/7/8: default schedules on onboarding, the local-presence check family, and
the read-only AdsPort. Schema: **migration 011** (010 is taken by D2 — verify before writing).
Style/test rules per docs/phase-a-contracts.md; ZERO network in tests (httpx MockTransport,
fake clients); DB tests under the DATABASE_URL skip guard; local pg16 per phase-c-wave2
COMMON. **No LLM anywhere in this wave** — WP-F's checks are graded by deterministic Python
overrides and never reach the classifier. Empty-state law throughout: missing data renders an
honest note ("awaiting ad account", "no local-pack sighting"), never zeros, never inventions.

> **WP-G is BLOCKED-ON-CLIENT** (HANDOFF §2.6): no client ad account exists. Build port +
> adapters + job against recorded fixture shapes ONLY; never run live; e2e deferred until a
> client links an account. The receipt line ships NOW, rendering "awaiting ad account"
> honestly. This blocks nothing in WP-E/WP-F.

## COMMON — schema (migration 011, WP-G owns the file), cadences, honesty rules

```sql
create table ads_daily (
  id bigint generated always as identity primary key,
  org_id uuid not null references orgs(id), site_id uuid not null references sites(id),
  date date not null, channel text not null check (channel in ('google_ads','meta_ads')),
  campaign_id text not null default '', campaign_name text,
  spend numeric(12,2) not null default 0, currency text not null default 'AED',
  clicks int, platform_conversions numeric(12,2),   -- NULL = provider had nothing
  pulled_at timestamptz not null default now()
);
create index ads_daily_slice_idx on ads_daily (site_id, date desc, channel);
-- + RLS block (007's org_isolation pattern). NO unique index: ingest is slice replacement
-- (DELETE the (site_id, date, channel) slice + INSERT — gsc_daily's discipline). Same file:
-- queue_items kind check += 'local_presence' (drop/re-add mirroring 008/010; the list must
-- carry 010's 'competitor_candidate').
```

Cadence constants (gm/core/schedules.py, the only copies): `DAILY=1440`, `WEEKLY=10080`,
`MONTHLY=43200` (D2's 30-day convention). Jobs are leased + idempotent, site-scoped via
job.site_id/payload.site_id, org_id resolved from sites when absent (handle_keyword_gap's
pattern). Money honesty: never sum spend across currencies — mixed-currency periods render
per-currency lines; a 0-denominator ratio renders "not computable", never 0 and never ∞.

## WP-E — default schedules: gm/core/schedules.py + cli.py wiring

Owns: NEW `gm/core/schedules.py`, `gm/cli.py` (sole owner), NEW `tests/test_default_schedules.py`.

```python
DEFAULT_SCHEDULES: tuple[tuple[str, int], ...] = (        # unconditional; payload {}
    ("track_serps", WEEKLY), ("keyword_gap", MONTHLY),    # keyword_gap/refresh tolerate empty
    ("assemble_receipt_monthly", MONTHLY), ("refresh_competitor_profiles", MONTHLY),
)                                                         #   competitor_domains (zero-spend note)
CONDITIONAL_SCHEDULES = {                                  # created only when the connection exists
    "send_lead_card": (("whatsapp",), WEEKLY),             # else the weekly job just fails noisily
    "pull_ads_daily": (("google_ads", "meta_ads"), DAILY), # any-of kinds, status='ok'
}
def first_run_at(job_type, *, today: dt.date | None = None) -> dt.datetime
    # deterministic (today injectable): most jobs -> now; send_lead_card -> next Monday 06:00
    # UTC (D1 weeks are Mon-start); assemble_receipt_monthly -> 1st of next month 06:00 UTC
def ensure_default_schedules(conn, *, org_id, site_id, today=None) -> dict
    # {"created": [job_type...], "existing": [...], "skipped": {job_type: reason}}. IDEMPOTENT:
    # any schedules row with (site_id, job_type) counts as existing — never duplicated, never
    # mutated (operator-tuned every_minutes/payload respected).
def backfill_default_schedules(conn, *, org_id, dry_run=False) -> dict
    # every non-control site in org; {"sites": {domain: ensure-result}}; dry_run reports only
def handle_assemble_receipt_monthly(ctx) -> None
    # assemble_receipt REQUIRES an explicit period payload (receipts.py determinism rule), so a
    # fixed-payload schedules row cannot drive it directly. This thin job derives period = the
    # calendar month BEFORE ctx.job.created_at (created_at, not now() — retries pin the same
    # month), then jobs.enqueue('assemble_receipt', site_id=..., payload={"period": p},
    # idempotency_key=f"receipt:{site_id}:{p}"). Missed months are NOT auto-backfilled
    # (catch-up ticks collapse); the operator runs the explicit verb instead.
```
cli.py: `gm site add` gains `--no-schedules` (opt-out; default wires ensure_default_schedules
after add_site, echoing created/skipped); new verb `gm site backfill-schedules [<domain>]
[--all] [--dry-run]` for existing sites; `gm wa-connect` and `gm ads connect` re-invoke
ensure_default_schedules so conditional schedules appear the moment their connection does;
worker() registers `assemble_receipt_monthly` + `pull_ads_daily`; a small `gm ads` sub-app
(connect/pull/status) — WP-G consumed strictly by the signatures below, lazy-import wrappers
à la _lazy_send_lead_card. Tests: idempotence (double add, tuned-row preservation), opt-out,
conditional matrix, first_run_at goldens, monthly handler period derivation incl. retry
stability + idempotency key, backfill dry-run, CLI verbs via runner. DB skip guard.

## WP-F — local-presence check family: registry data + deterministic evaluators

Owns: `registry/checks/j.json` + `registry/manifest.json` + `registry/README.md` (append),
`gm/audit/registry.py` (VALID_FIX_TYPES append), `gm/audit/pipeline.py` (override hook +
DRAFT_NA ids), `gm/intel/serp.py` + `gm/intel/detectors.py` (both append-only),
NEW `gm/intel/local_presence.py`, NEW `tests/test_local_presence.py`.

Registry data (category J "Entity Consistency" is the home — a new letter K would mean
CATEGORY_LETTERS/loader/scoring surgery for three checks; rejected). **Minting rules:** new
ids are J-05..J-07 (J-01..J-04 exist), `check_version: 1`; ids are append-only forever — the
C-13 gap rule in registry/README.md is binding: never renumber, never fill gaps; future
criteria changes bump check_version (delta comparability is per check). manifest.json:
v1.3.0 → **v1.4.0**, categories J 4 → 7, checks_total 103 → 106. README gains a caveat:
J-05..07 added in D3; brain-mappings has no entries for them (lookups tolerate absence —
reports render the checks' inline sources). registry.py: `VALID_FIX_TYPES += 'local_listing'`
(the directory-listing fix class, roadmap Phase D §6; additive, existing checks untouched).

| id | name | method/badge | weight/sev | fix_type | pass / warn / fail / inconclusive |
|---|---|---|---|---|---|
| J-05 | GBP presence & completeness | deterministic/measured | 2/medium | local_listing | your newest pack entry has title+rating+votes+phone+url / ≥1 core field absent / packs observed for tracked queries but you appear in none / no pack (or only legacy no-entry snapshots) observed |
| J-06 | NAP consistency across sightings | deterministic/measured | 2/medium | local_listing | one folded title+phone identity, domain matches domain_norm / minor variants (case/whitespace only) / conflicting phone or name or foreign domain / <2 attributable sightings |
| J-07 | review-signal vs local pack | deterministic/measured | 1/low | offpage_entity | rating ≥ pack median AND votes ≥ pack median / below either / rating < median−0.5 or votes < ½·median / you or competitor ratings unobserved |

**DIAGNOSTIC ONLY (do-not-build list, docs/01-product.md §6):** J-07 measures review signal;
the platform never generates, drafts, solicits, templates, or automates reviews. fix_template
texts point the operator at the client's own legitimate in-person review-request process; no
fix-closer path may consume J-05..J-07 (both fix classes are operator-executed, like levers).

serp.py append (D2's owner-retention pattern): `_normalize_items` retains local_pack ENTRIES
going forward — the feature gains `"entries": [{"rank","title","domain","url","phone",
"rating","votes","is_paid"}]` (each field only when the provider sent it). Old snapshots lack
entries and the raw response is unstored (D0 note) — extraction counts them "pack present,
entries unobserved", never guesses. Zero new spend: pure DB reads over snapshots already
bought weekly.

local_presence.py:
```python
def collect_local_pack(conn, site_id, *, window_days=35) -> dict
    # newest in-window snapshot per active tracked query; you-match = entry title in brand_terms
    # (case-folded) OR entry domain/url host matches domain_norm (subdomain-aware) ->
    # {"sightings": [{"query_norm","fetched_at","entries","you": entry|None}],
    #  "packs_seen", "packs_unobserved", "you_seen", "note": str|None}
def evaluate_local_presence(pack: dict, site: dict) -> dict[str, dict]
    # PURE: {check_id: {"status","note","source":"deterministic"}} per the table above;
    # notes carry the numbers ("in pack for 2 of 5 tracked queries")
def local_presence_overrides(conn, site_id, registry) -> dict[str, dict]   # pipeline hook
def detect_local_presence(conn, site_id) -> int    # queue upserts, returns items touched
```
Audit surfacing: pipeline.py merges local_presence_overrides into the pre-decided overrides
(comparative_na_overrides' mechanism) for client-site page audits, so J-05..07 NEVER reach the
classifier — zero LLM spend, byte-identical statuses. competitor_reference/comparison audits
get `na` ("client-site diagnostic"); J-05..07 join DRAFT_NA_CHECK_IDS (draft evidence has no
conn/site). Scoring, badges, receipt findings-movement flow free — ordinary audit_findings
rows. Queue surfacing: detect_local_presence upserts kind='local_presence' via
detectors._upsert_item on fail/warn — target={"check_id"}, at_stake={"issue",
"queries_with_pack","basis":"serp_local_pack"} — inheriting open-refresh/dismissed-snooze/
actioned discipline; detectors.compute_queue appends a lazy-import call (tolerate absence, D0
_rank_movement_fn pattern). Tests: entry retention (new vs legacy rows), newest-per-query
window, you-match matrix (brand term, subdomain, foreign host), full status matrix per check
incl. EVERY inconclusive branch, median determinism, proof J ids never appear in classifier
prompts (FakeLlm asserts), draft/reference na, queue upsert + snooze re-run, load_registry
golden (v1.4.0, 106 checks, new fix_type accepted). DB skip guard.

## WP-G — AdsPort read-only ROAS: migration 011 + ads.py + ads_ingest.py + receipt line

Owns: `ops/migrations/011_phase_d3_ads_local_queue.sql` (COMMON), NEW `gm/connections/ads.py`,
NEW `gm/intel/ads_ingest.py`, `gm/delivery/receipts.py` (sole owner), NEW `tests/test_ads_port.py`.

ads.py — **read-only by construction** (architecture §6): read scopes and report/insights
endpoints only; no mutate/write call exists anywhere in the module (a test greps the source
for 'mutate' and write-path URL fragments — cheap, deterministic, documents the guarantee):
```python
class AdsError(Exception): retryable                       # 429/5xx retryable; 401/403 not
class AdsReader(Protocol):
    channel: str                                           # 'google_ads' | 'meta_ads'
    def daily_rows(self, *, since: dt.date, until: dt.date) -> list[dict]: ...
        # [{"date","campaign_id","campaign_name","spend": float (currency units),
        #   "currency","clicks": int|None,"platform_conversions": float|None}]
class GoogleAdsReader:                                     # manager-link pattern, READ scope
    def __init__(self, *, customer_id, login_customer_id, client: httpx.Client | None = None)
    # env GOOGLE_ADS_DEVELOPER_TOKEN + REFRESH_TOKEN/CLIENT_ID/CLIENT_SECRET; POST
    # googleAds:searchStream, GAQL over campaign: segments.date, campaign.id/name,
    # metrics.cost_micros (÷1e6 → spend), clicks, conversions
class MetaInsightsReader:                                  # BM analyst user, ads_read only
    def __init__(self, *, act_id, token=None, client: httpx.Client | None = None)
    # env META_ADS_TOKEN; GET graph.facebook.com/v20.0/act_{id}/insights level=campaign
    # &time_increment=1&fields=spend,clicks,actions&time_range=...; paginated
def readers_for_site(conn, site_id) -> list[AdsReader]
    # connections kind ∈ (google_ads, meta_ads) status='ok'; meta carries {"customer_id",
    # "login_customer_id"}/{"act_id"}; credentials NULL — tokens in env only (wa-connect precedent)
```
ads_ingest.py (gsc_ingest's shape): `pull_ads_daily(conn, *, org_id, site_id, readers=None,
days=7, today=None) -> dict` — trailing-window re-pull (platforms restate conversions),
slice replacement per (site, date, channel), cost_event per pull (provider=channel,
purpose='ads_daily_pull', cost 0 — audit trail, the APIs are free); non-retryable auth errors
set connections.status='broken' + last_error, reported honestly. No connections ⇒
`{"note": "no ads connections"}`, zero work. `handle_pull_ads_daily(ctx)` job wiring per
COMMON; worker registration + CLI + default schedule are WP-E's.

receipts.py: assemble_site_receipt gains `payload["paid_media"] = roas_lines(...)`:
```python
def roas_lines(conn, site_id, period) -> dict
    # {"status": "awaiting_ad_account"|"no_spend_recorded"|"ok",
    #  "channels": [{"channel","spend","currency","clicks","platform_conversions"}],
    #  "booked_consults": n,                    # booked_leads in the period (all sources)
    #  "blended_cost_per_consult": float|None,  # sum(spend)/booked; None when booked==0 OR
    #  "prior": {...same shape}|None, "note"}   #   currencies are mixed — note says which
```
render_receipt_html: a "Paid media" section directly before the BETA citation section —
status='awaiting_ad_account' renders the honest one-liner **"Blended cost per booked consult:
awaiting ad account connection"** (no table, no zeros); 'no_spend_recorded' says so; 'ok'
renders per-channel rows + the blended line ("not computable — 0 booked consults this period"
when None) + prior-period trend arrow only when both periods computed; every value through
report._esc. Tests (fixtures ONLY — the blocked-on-client rule): searchStream + insights
parsing (paging, missing metrics → None), micros conversion, retry/backoff with _sleep patched,
401 → broken connection, slice-replacement idempotence (double pull, one slice), mixed-currency
law, roas_lines all states, receipt golden per rendering incl. hostile-string escaping and the
divide-by-zero guard. DB skip guard.

## Integrator notes
Disjoint by construction: cli.py = WP-E only; serp.py/pipeline.py/detectors.py/registry data =
WP-F only; migration 011/receipts.py/ads modules = WP-G only; cross-WP use strictly by the
signatures above. Ordering: D2 (migration 010, refresh_competitor_profiles) lands first —
011's queue-kind list and WP-E's DEFAULT_SCHEDULES assume it. e2e before done: one real
`gm site add` + backfill (schedules verified in the DB); one collect_local_pack run against
prod snapshots ($0 — expect "entries unobserved" until the next weekly track_serps tick buys
entry-bearing snapshots) then a real audit showing J-05..07 statuses; WP-G e2e DEFERRED
(blocked on client) — instead render the live receipt and verify the "awaiting ad account"
line. Evidence under ops/evidence/.
