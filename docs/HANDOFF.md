# HANDOFF — state of the platform and what remains

Written 2026-07-04 for the next implementer (human or agent). Everything below is verifiable:
every "done" claim has a test, a CI run, or a live production artifact behind it. Read this,
then `docs/01-product.md` (what we sell and why), `docs/02-architecture.md` (reviewed design),
`docs/03-roadmap.md` (gates + kill criteria). The contracts docs (`docs/phase-*-contracts.md`)
are the binding interfaces each module was built against — extend them, don't bypass them.

## 0. Orientation in 60 seconds

One engine: **audit → compare → brief → draft → grade → publish → verify → measure → receipt**,
for the GCC clinic-group concierge wedge. LLMs classify and generate; **Python grades and
scores, deterministically** — that is the product's credibility claim and is non-negotiable.
Everything runs as leased jobs on Postgres. Every claim a client sees carries evidence
(truth badges, citations, provenance FKs) and honest failure states — never fake zeros,
never invented facts, never binary before/after claims.

- Repo: this one. `platform/` (Python 3.12, package `gm`), `registry/` (103-check ruleset +
  12,764-entry citations brain — DATA, not code), `ops/` (9 migrations, runbooks, evidence),
  `docs/` (specs/contracts).
- Prod: Railway project `growthmonk-fable` (id 0365f2fa-be15-4d34-9045-5a102a3e6de2):
  Postgres + `worker` (all 18 job types) + `api` (share pages, admin, WhatsApp webhook) at
  `api-production-e922.up.railway.app`. Push to `main` auto-deploys both services. CI runs the
  full suite against Postgres 16 on every push — currently **685 tests, zero skips**.
- Local dev: `python3.12 -m venv .venv && .venv/bin/pip install -e "./platform[dev]"`. Local
  pg16 recipe (no Docker on this machine): `initdb --no-locale -E UTF8 -D /tmp/gm_pg16 -U postgres`
  (the `-E UTF8` matters: SQL_ASCII makes psycopg return bytes and the migration tracker re-runs 001),
  start on port 54329 with `-c unix_socket_directories=/tmp -c listen_addresses=localhost`,
  `DATABASE_URL=postgresql://postgres@localhost:54329/growthmonk`. Full suite: `ruff check
  platform && pytest platform/tests -q`.
- Operator surface: `gm --help` (28 top-level entries, 37 leaf commands). The whole loop
  is drivable from the CLI.

## 1. DONE — with the live proof for each

| Capability | Live proof |
|---|---|
| Citation proof engine (Gate-2): 3-engine sampling, frozen panels, Wilson-CI gate verdicts, evidence logs | prod dry-runs 9/9 ok; `ops/evidence/2026-07-03-smoke.growthmonk.ai.md` |
| 103-check audit: deterministic scoring, $2.50 cap, truth badges, brain citations (75/103 checks source-backed) | growthmonk.ai audited **C+ (71.5), $0.59, ~4 min** — served live at `/r/<token>` w/ strict CSP |
| Group autopsy rollup (sitewide-vs-per-location fix queue) | CI-tested at exact ceil(60%) boundaries |
| Client-forwardable report design (grade stamp, fix-queue-first, "why this matters" citations) | the live share page; artifact `autopsy-report-live` |
| Comparative audits (our checks on competitor pages, `competitor_reference`-tagged) | live: 2 precise gaps vs ahrefs/partnerstack for "ai visibility audit" |
| Brief generator (deterministic assembly + advisory synthesis) | `ops/briefs/2026-07-03-growthmonk.ai-ai-visibility-audit.md` |
| Fix-closer + **convergence fix proven**: real author entity + human signals enforced | engine **86→92 "Publish-ready"** vs the 13 pre-fix runs' 42–51 plateau; 4,892-word draft; our scorecard C+ 71.8; root-cause in `docs/convergence-diagnosis.md`. Caveat: the diagnosis projects ~82–86 from input fixes alone — sustained ≥90 needs the blog-buster `faqVisibleCount` scoping fix (§2 known debt); treat the single 92 as within judge variance until re-proven |
| Draft audits (pre-publish scorecard, honest N/A sets) + comparative-N/A token optimization | test_draft_audit.py; wired into fix-closer |
| GSC: vault, service-account client, two-phase ingest, 4 detectors, operator queue | full DB test coverage; **never run against real GSC data** (see §3 operator items) |
| WordPress publish + verify (preflight, IndexNow, BEV re-probe, GSC inspect) | code-complete + tested with fixtures; **never run against a real WP site** |
| Delta Receipt v1 (FK provenance, comparable-only diffs, BETA citation section, claim ceiling) | `ops/receipts/2026-07-growthmonk.ai.html` (honestly sparse — no GSC yet) |
| D0: rank + AI-Overview tracking, keyword-gap detector w/ topic-relevance filter, SERP context in audits/receipts | live: 3 queries tracked ($0.004, cache hit proven), gap run 209→15 after filter ($0.06) |
| D1: signed WhatsApp webhook → booked_leads (click-to-chat attribution, sha256 contact refs), weekly trend card | live simulated: handshake/signature/replay all verified; card rendered "Booked consults this week: 1 (▲ from 0)" |
| One-platform lead view | `gm lead add/list/card`, `/admin/sites/{id}/leads` |

## 2. REMAINING — ordered backlog for the next implementer

Rule zero: **respect the gates.** `docs/03-roadmap.md` §kill-criteria and `docs/01-product.md`
§do-not-build override any instinct to build ahead. When a wave adds a migration, take the
next free number — check `ops/migrations/` first (an 007/008 near-collision during the D
waves was resolved; the tree is clean 001–009, next free is 010+whatever has landed since).

### D2 — Competitor intelligence pack (recommended next; ~1 wave)
The DataForSEO integration covers the wedge loop but NOT the "competitor overview" layer.
Add, all via existing `LabsClient`/`serp` patterns and reuse-before-buy caching:
1. `domain_rank_overview` + `bulk_traffic_estimation` per competitor → a `competitor_profiles`
   table (their total keywords, est. traffic, movers) refreshed monthly.
2. `domain_intersection` for systematic competitor DISCOVERY (today competitors are hand-picked
   via `gm site set-competitors`) — propose candidates, operator confirms.
3. SERP-feature share over time (who owns PAA/featured snippets/AIO for the tracked panel) from
   snapshots already being bought weekly — pure assembly, zero new spend.
4. Receipt/report: a "competitive position" section (you vs each competitor: rank count,
   AIO citations, audit-score medians). This is the "show a lot vs comp" ask — the data layer
   for it is 70% already in `serp_snapshots`/`rank_history`/`audits(competitor_reference)`.
5. Optional depth: top-100 SERP depth for page-2 striking-distance (cost doubles per extra 100
   results — keep depth=10 default, per-query opt-in).

### D3 — Phase D tail (from docs/03-roadmap.md)
6. AdsPort read-only ROAS: `ads_daily` + `pull_ads_daily` (Google Ads manager-link + Meta BM
   analyst patterns per `docs/02-architecture.md` §6) → blended cost-per-booked-consult receipt
   line. **Blocked on a client ad account** — build the port + fixtures, mark the receipt line
   "awaiting ad account" honestly.
7. Local-presence check family in `registry/` (GBP completeness, NAP consistency, review-signal
   scoring — diagnostic only, never review generation). The `local_pack` data already arrives in
   SERP snapshots; add extraction + checks + fix class. Bump per-check versions properly.
8. Default schedules: weekly `track_serps` + `send_lead_card`, monthly `assemble_receipt` +
   `keyword_gap` — wire into `gm site add`/onboarding so new sites get the full cadence.
9. Proof Pack white-label — **GATED: do not build before the Oct 15 ≥5-paid-cards gate.**

### E — Productization (gated on the winning lane; see roadmap Phase E)
Stripe + plan states · staged self-serve onboarding (needs Google OAuth verification —
paperwork rail, start early) · per-org LLM budget enforcement (cost_events already recorded) ·
citation variance calibration study → de-BETA the receipt citation section · embeddings
features (vector column exists) · customer-facing queue/receipts UI · nightly eval automation.

### Known debt (fix opportunistically, don't ship features on top of it)
- Blog-buster `faqVisibleCount` scoping bug (validators.ts, counts page-wide FAQ instead of
  section-scoped): permanent −14 on fix-closer verification; `docs/convergence-diagnosis.md`
  §3.2/§5 calls it the only upstream code fix worth making. Until it lands, do not raise the
  fix-closer gate above 85.
- Content engine (serp-analyzer) runs on the operator machine via `CONTENT_ENGINE_URL`;
  Railway deploy deliberately deferred (vendoring decision) — document in every fix-closer run.
- Railway PG connects as owner → RLS policies exist but aren't FORCE-enforced (fine solo;
  role-split before any second operator).
- Keyword-gap relevance filter is single-token overlap (v1); tighten to bigrams if
  giant-publisher competitors stay in use. Same-vertical competitors are the intended config.
- `queue` CLI/report surfaces `est_clicks_gain` and `volume` heterogeneously; unify at_stake
  presentation when building the customer-facing queue.
- Credential rotations owed: Railway workspace token, Anthropic key (sieve-crawler), DataForSEO
  password — all appeared in chat transcripts.

## 3. OPERATOR items (not code — nothing above unblocks these)
1. **Lock `ops/gate1-thresholds.yaml`** (status DRAFT; deadline was Jul 7) + seed the real
   panel (treatment + control sites, 5 prompts each, weekly schedules) — Gate-1 evidence is
   serial; every idle week slips the Sep 1 verdict.
2. Railway env: OPENAI/PERPLEXITY/GEMINI_API_KEY (sampling), ANTHROPIC_API_KEY (audits),
   WABA_* ×4 + Meta webhook registration (`/webhooks/whatsapp`) + `gm wa-connect` per site.
3. GSC: create the service account, grant it on client properties, `gm gsc connect`.
4. DataForSEO: $50 deposit when client volume starts (trial ~$0.83 left).
5. WordPress: first real client site → run `gm wp-connect` preflight → fixes any host quirks.

## 4. How to work on this codebase (the pattern that built it)
Contracts-first: write/extend a `docs/phase-*-contracts.md` with exact signatures BEFORE
coding; build modules against contracts with disjoint file ownership (this enabled 2-5 parallel
builders per wave with zero collisions); everything gets fixture tests (zero network — httpx
MockTransport, FakeLlm, fake fetchers) + DB tests under the skip guard; **e2e against production
before calling anything done** — the live runs caught real bugs fixtures missed (YAML colon,
$PORT expansion, epoch-year test data, keyword-gap noise). Never mark a check/status the
evidence can't support; prefer an honest `inconclusive`/`na`/empty-state over a guess.
