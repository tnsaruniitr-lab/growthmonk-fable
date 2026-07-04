# Phase D4 module contracts — operator console · spend rail · refusal log · hardening

Closes roadmap Phase D.7 (refusal log) plus three HANDOFF §2 known-debt items (heterogeneous
at_stake presentation, single-token keyword-gap filter, owner-connection RLS), and grows Phase
B.7's minimal internal admin into a real operator console. Schema: **migration 012** (011 is
the last landed — verify before writing). Style/test rules per docs/phase-a-contracts.md; ZERO
network in tests (httpx MockTransport per test_labs.py); DB tests under the DATABASE_URL skip
guard; local pg16 per HANDOFF §0. **No LLM anywhere in this wave.** Empty-state law: a
`count(*)` zero is an honest zero; a rate/median/share with a zero denominator is **None**,
rendered "no data yet" — never 0, never invented.

> **Do-not-build check (docs/01-product.md §6):** the list forbids a score/monitoring dashboard
> **as a product surface** — sold, shared, or score-led. WP-H is NOT that: it is Phase B
> deliverable 7's explicitly sanctioned founder-only internal admin ("support tooling") grown to
> Phase-D scope — token-gated behind ADMIN_TOKEN, never linked from reports/receipts, no share
> tokens, no theming. Customer-facing UI stays **Phase-E-gated**.

## COMMON — migration 012 (WP-J owns the file), env, sequencing

```sql
create table refusals (                    -- roadmap Phase D.7: the >50% DIY-refusal
  id uuid primary key default gen_random_uuid(),  -- early-death tripwire needs data
  org_id uuid not null references orgs(id),
  prospect text not null,                  -- who said no (clinic/agency name, free text)
  source text not null default 'agency_pitch',    -- pitch channel, free text
  reason text not null check (reason in ('diy','price','timing','trust','other')),
  notes text, refused_at date not null default current_date,
  created_at timestamptz not null default now()
);
create index refusals_org_time_idx on refusals (org_id, refused_at desc);
-- + RLS per 007's pattern (enable RLS + org_isolation policy). Nothing else in this file.
```

New env (all optional; absence is an honest state, never an error): `GM_DFS_MONTHLY_BUDGET_CENTS`
(int; unset = no cap), `RCLONE_CONFIG_*` + `GM_OFFSITE_REMOTE` (backup off-site hook); existing
`ADMIN_TOKEN` and `DATAFORSEO_LOGIN`/`PASSWORD` reused, never renamed. WP-H/I/J run in parallel
with disjoint files; **WP-WIRE lands strictly after all three** as sole owner of `gm/cli.py` +
`gm/api.py` — no other WP touches those two files. Cross-WP consumption is by signature only,
via lazy imports + honest fallbacks (D0's `_rank_movement_fn` pattern) where a sibling may lag.

## WP-H — internal operator console: gm/delivery/console.py

Owns: NEW `gm/delivery/console.py`, NEW `tests/test_d4_console.py`.

A FastAPI `router = APIRouter()` (api.py includes it in WP-WIRE) with its own `require_admin`
dependency — api.py's `_require_admin` semantics (X-Admin-Token vs env ADMIN_TOKEN,
`secrets.compare_digest`, 404 when unset or wrong) as a LOCAL COPY of the underscore name
(labs.py precedent) — on every JSON endpoint. `GET /admin/ui` serves the page WITHOUT the
header (browsers can't send headers on navigation) but still 404s when ADMIN_TOKEN is unset;
the shell holds zero tenant data — everything arrives via fetch with `X-Admin-Token`, prompted
ONCE and kept in localStorage (`gm_admin_token`; any data-endpoint 404 clears it and re-prompts
— wrong token stays indistinguishable from an absent surface). Headers: noindex, no-referrer,
CSP `default-src 'none'; style/script-src 'unsafe-inline'; connect-src 'self'`. One
self-contained HTML string: no build step, no external CDNs/fonts/images, system font stack
(report.py's `.sans`/`.mono`), dark AND light (`color-scheme: light dark` +
`prefers-color-scheme` variables). Restrained: generous whitespace, tables not cards, color
accents ONLY for status (green ok / amber waiting / red failed), no emoji; every dynamic value
rendered via a `textContent` escaper, never innerHTML with data (report.py's posture,
client-side). Sections, each an anchor:

1. `#overview` — the "what is this machine doing" view for an operator who doesn't yet know
   the workflow: **audit→compare→brief→draft→publish→verify→measure→receipt→prove** as
   HTML/CSS steps (flex row, CSS arrows, no SVG), a one-line plain-English caption per stage
   ("we grade the page against 106 checks", …), live counts from `GET /admin/overview`.
2. `#sites` — `GET /admin/sites/overview` table: domain, org, is_control, active tracked
   queries/prompts, competitor count, last audit grade + when (None → "never audited"),
   schedules (job_type / cadence / next_run_at / enabled).
3. `#jobs` — `GET /admin/jobs/recent?limit=50` (any status) + existing `/admin/jobs/dead`;
   dead rows get a retry button POSTing the existing `/admin/jobs/{id}/retry`.
4. `#queue` — `GET /admin/queue`: queue_items by kind/status, unified at_stake via WP-J's
   `normalize_at_stake` (lazy accessor; absent → raw JSON + "display normalizer not deployed").
5. `#citations` — `GET /admin/citations/summary`: recent runs (status, ok/err samples),
   per-prompt cited/mentioned rates pooled across engines (**error samples excluded from
   numerator AND denominator** — gate1-thresholds rule); Gate-1 progress per treatment site:
   baseline runs done of 3 / treatment done of 3 (split at the site's FIRST levers.applied_at;
   no lever → all baseline, treatment says "no lever logged yet"), days to the Sep 1 verdict.
6. `#spend` — fetches `GET /admin/spend` (WP-WIRE, backed by WP-I): per-provider/purpose/day
   tables, live DataForSEO balance ("unreachable" honored), projected monthly burn, budget bar
   (no cap → "no cap configured", never a fake bar); 404 before WIRE lands → "not wired yet".

```python
def overview_data(conn, *, now: dt.datetime | None = None) -> dict   # PURE of HTTP; testable
    # {"sites": {"total","control"}, "stages": [ordered {"id","label","caption","counts",
    #                                                    "note": str|None}]:
    #    audit   {"audits_this_month", "median_score": float|None}    # done, draft_id null,
    #    compare {"comparative_audits_this_month"}                    #   non-excluded gates
    #    brief   {"briefs_this_month"}         draft  {"drafts_in_flight"}   # content_items
    #    publish {"publish_events_this_month"} verify {"verify_events_this_month"}
    #    measure {"tracked_queries","booked_leads_this_week","latest_gsc_final": date|None}
    #    receipt {"receipts_assembled": site_deltas rows, "latest_period": str|None}
    #    prove   {"prompts_tracked","runs_this_week","samples_ok","samples_err"}
    #  "queue_open_by_kind": {kind: n},
    #  "next_jobs": [{"job_type","site": domain|None,"next_run_at","eta_minutes": int}]}
    # Weeks Mon-start (D1 convention); months calendar; `now` injectable for determinism.
```
Endpoints are thin wrappers over pure `*_data(conn)` helpers in the same module (api.py's
connection-per-request discipline; read-only work ends with rollback). Tests: require_admin on
EVERY new endpoint (no header / wrong header / ADMIN_TOKEN unset → all 404); overview_data full
shape + honest empty states on a fresh migrated DB (None medians, true-zero counts); /admin/ui
smoke: 200 with token, 404 without, all six anchors present, zero external URLs (grep src/href
for `http` — cheap, no browser). DB skip guard.

## WP-I — DataForSEO consumption + budget rail: gm/intel/spend.py + labs.py/serp.py

Owns: NEW `gm/intel/spend.py`, `gm/intel/labs.py`, `gm/intel/serp.py` (both surgical), NEW
`tests/test_d4_spend.py`, `tests/test_labs.py` (gap-filter cases; justify changed expectations).

```python
class BudgetExceeded(Exception):           # typed refusal; carries cap_cents, spent_cents;
    retryable = False                      #   message = the honest operator-facing note
def spend_rollup(conn, *, days: int = 30) -> dict
    # cost_events over the window: {"window_days", "total_cents",
    #  "by_provider": [{"provider","cost_cents","events"}] / "by_purpose": [+"purpose"], both
    #  cost desc; "by_day": [{"date","provider","cost_cents"}] chronological,
    #  "last_event": {"created_at","provider","purpose","cost_cents","units"}|None}
def dataforseo_balance(client: httpx.Client | None = None) -> dict
    # GET https://api.dataforseo.com/v3/appendix/user_data — serp.py's Basic-auth env (free,
    # no cost_event); parses tasks[0].result[0].money.balance (dollars). {"balance":
    # float|None, "note": str|None} — None + note on missing env, transport failure, or bad
    # envelope. NEVER raises into callers.
def budget_state(conn, *, now: dt.datetime | None = None) -> dict
    # {"cap_cents": int|None (env GM_DFS_MONTHLY_BUDGET_CENTS; unset/blank -> None),
    #  "spent_cents": float (sum cost_events provider='dataforseo' this CALENDAR month),
    #  "projected_month_cents": float|None (spent/days_elapsed*days_in_month; None w/o spend),
    #  "exceeded": bool (False when cap is None), "note": str|None ("no cap configured")}
def require_dfs_budget(conn) -> None   # raises BudgetExceeded when exceeded; checked BEFORE
```                                    # spending, never after — a refusal costs $0
Guard wiring (paid call sites in owned files ONLY): serp.py `get_snapshot`/`get_volumes` call
`require_dfs_budget(conn)` on the **purchase path only** (cache hits stay free, unguarded);
labs.py `keyword_gap` checks once before its competitor loop, and on BudgetExceeded returns
`{"competitors": [...], "candidates": 0, "queued": 0, "cost_cents": 0.0, "note": exc's message}`
— the job records the refusal in its result, never silently skips. Elsewhere it propagates:
`track_serps` etc. fail with the note in `jobs.last_error` (retryable=False → dead after cheap
no-spend retries — visible, honest). serp/labs lazy-import gm.intel.spend inside the purchase
path (no cycle). D2's competitors/discovery purchases untouched (see integrator notes).

**Keyword-gap relevance filter v2** (labs.py; HANDOFF debt "single-token → bigrams"):
`_relevance_terms` becomes `_relevance_signal(conn, site_id) -> {"tokens": set[str],
"bigrams": set[str]}` — tokens filtered as today (len ≥ 3, minus _RELEVANCE_STOPWORDS),
bigrams = adjacent raw-token pairs ("dental clinic") from tracked queries + brand_terms. Keep
rule: (1) any adjacent candidate bigram ∈ signal.bigrams → keep; else (2) overlap ratio
|candidate_tokens ∩ tokens| / |candidate_tokens| ≥ `RELEVANCE_THRESHOLD = 0.5` (module
constant: at least half the candidate's meaningful tokens on-topic) → keep; (3)
single-meaningful-token candidates, or a signal with NO derivable bigrams, fall back to v1's
any-token-overlap rule; (4) empty signal passes everything (unchanged: no signal = no filter).
Tests: rollup math over planted cost_events; balance via MockTransport (success / bad envelope
/ transport error / env unset — all honest Nones); budget matrix (no cap, under, over, exact
cap) + keyword_gap refusal note + get_snapshot cache-hit-free vs purchase-refused; filter
before/after: bigram keep, 0.5 ratio boundary, single-word fallback, empty-signal pass-through,
and a 209→15-style giant-publisher noise fixture proving v2 filters off-topic queries at least
as hard as v1 while keeping every on-topic v1 survivor.

## WP-J — refusal log + at_stake unification + ops hardening

Owns: NEW `ops/migrations/012_phase_d4_refusals.sql` (COMMON), NEW `gm/core/refusals.py`,
`gm/intel/detectors.py` (append-only), `ops/backup/backup.sh` + `ops/runbooks/backups.md`,
NEW `ops/runbooks/rls-role-split.md` + NEW `ops/scripts/create_worker_role.sql`,
NEW `tests/test_d4_refusals.py`, `tests/test_detectors.py` (append the normalize matrix).

```python
# gm/core/refusals.py — the agency-pitch tripwire ledger (roadmap D.7)
def add_refusal(conn, *, org_id, prospect: str, reason: str, source: str = "agency_pitch",
                notes: str | None = None, refused_at: dt.date | None = None) -> str
    # row id; reason validated against the check list BEFORE insert (typed ValueError)
def list_refusals(conn, *, org_id, days: int = 180) -> list[dict]      # newest first
def refusal_stats(conn, *, org_id, days: int = 180) -> dict
    # {"total", "by_reason": {every check-list reason: n}, "diy_share": float|None} — None
    # when total == 0: the tripwire must never read "no refusals logged" as "0% DIY".

# gm/intel/detectors.py append — unified at_stake PRESENTATION (raw at_stake rows untouched)
def normalize_at_stake(item: dict) -> dict     # pure; no conn, no I/O
    # queue_items row ("kind","target","at_stake") ->
    # {"kind","headline","detail","value": float|None,"unit": str|None}. Per kind:
    #  striking_distance/ctr_outlier/decay/cannibalization: value=est_clicks_gain,
    #    unit="clicks/mo", headline "+{value:g} clicks/mo", detail from position/ctr/drop_pct
    #  keyword_gap: value=volume, "searches/mo", detail "best: {best_competitor} at #{pos}"
    #  competitor_candidate: value=intersections, "shared keywords", detail from
    #    avg_position/their_etv ("no data yet" for absent fields)
    #  local_presence: value/unit=None, headline=at_stake["issue"], detail "packs on
    #    {queries_with_pack} tracked queries"
    # Unknown kind / missing fields -> value None, headline "at stake: not quantified",
    # detail = compact raw JSON — honest, never a fake zero.
```
`ops/backup/backup.sh` — optional off-site hook AFTER the existing integrity checks pass: if
any `RCLONE_CONFIG_*` env AND `GM_OFFSITE_REMOTE` (e.g. `offsite:gm-backups`) are set,
`rclone copyto "$out" "$GM_OFFSITE_REMOTE/$(basename "$out")"` — a failed copy exits non-zero
(fails loudly; the local dump is already safe); else print `off-site: not configured (set
RCLONE_* env)`, exit 0. No aws-cli, no curl sigv4 — rclone or nothing. backups.md: replace the
"Known limits" off-site paragraph with the hook setup + the rclone-binary image note.

`ops/scripts/create_worker_role.sql` + `ops/runbooks/rls-role-split.md` — **script + runbook
ONLY, no automatic prod cutover** (HANDOFF debt: Railway connects as owner, RLS unenforced).
Script: `create role gm_worker login`; grants (connect/usage, table CRUD, sequences, matching
`alter default privileges`); `force row level security` on EVERY table with an org_isolation
policy (listed explicitly; jobs/schedules/cost_events/orgs/quota_ledgers carry none and stay
plainly readable — the worker needs them org-less); verification queries (relforcerowsecurity;
a cross-org `set local app.org_id` select and a no-context select, both MUST return 0 rows).
Runbook, STAGED: (1) script on a scratch pg16 restore, full suite as gm_worker; (2) create
role on prod, verify grants read-only; (3) switch worker+api DATABASE_URL (migrations KEEP
the owner URL); (4) healthz + one job cycle; rollback = point DATABASE_URL back to owner (one
env edit, zero schema changes). Tests: refusals CRUD + stats honesty (None share at total=0,
share math, window edge, bad reason → ValueError); normalize_at_stake matrix over EVERY kind
— striking_distance, ctr_outlier, decay, cannibalization, keyword_gap, competitor_candidate,
local_presence — plus unknown-kind/missing-field rows: no fake zeros. DB skip guard.

## WP-WIRE — gm/cli.py + gm/api.py (sequenced AFTER H/I/J land)

Owns: `gm/cli.py`, `gm/api.py` (sole owner of both), NEW `tests/test_d4_wire.py`.

api.py: `app.include_router(console.router)`; `GET /admin/spend?days=30` → `{"rollup":
spend_rollup(conn, days=days), "balance": dataforseo_balance(), "budget": budget_state(conn)}`;
`GET /admin/refusals?days=180` → `{"refusals": list_refusals(...), "stats": refusal_stats(...)}`;
`POST /admin/refusals` (JSON: prospect/reason required; source/notes/refused_at optional) →
add_refusal, 400 with the typed message on a bad reason. All three carry the existing `_admin`
dependency list and run org-less; org_id = the sole org (cli.py's `_org` rule — exactly-one-org
holds through Phase D). cli.py: `gm spend [--days 30]` — rollup tables + balance line + budget
bar as text (`[####----] 42% of cap`; "no cap configured"; "balance unreachable" honored); new
`refusal_app`: `gm refusal add <prospect> --reason diy|price|timing|trust|other [--source]
[--notes] [--date]`, `gm refusal list [--days 180]`, `gm refusal stats` ("DIY share: no
refusals logged" when None — tripwire honesty); `gm queue` switches to `normalize_at_stake(row)`
(headline + detail columns; the ad-hoc `est_clicks_gain`/`volume` special-casing is deleted).
Tests: auth-guard trio per new api route; CLI happy paths via the Typer runner (spend over
planted cost_events, refusal add→list→stats round-trip); honest empty states (fresh DB:
true-zero vs None); queue rendering golden over one row of every kind. DB skip guard.

## Integrator notes
Disjoint by construction: console.py + test_d4_console.py = WP-H; spend.py/labs.py/serp.py +
test_d4_spend.py + test_labs.py = WP-I; migration 012 + refusals.py + detectors.py +
backup/runbook/role-split files + test_d4_refusals.py + test_detectors.py = WP-J; cli.py +
api.py + test_d4_wire.py = WP-WIRE, landing last, the only WP importing across all three.
Known follow-ups (documented, not built): budget guard on D2's competitors/discovery purchase
paths; EXECUTING the role-split runbook (before any second operator); rclone in the backup
image. e2e before done: /admin/ui against prod with the real token — every section renders
live data or its honest empty state; `gm spend` against prod (balance matches the DataForSEO
dashboard); one real `gm refusal add` + stats; one budget-refusal dry run (1-cent cap on a
scratch env → keyword_gap returns the note, $0 spent). Evidence under ops/evidence/.
