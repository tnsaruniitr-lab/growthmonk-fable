# Phased Engineering + Product Roadmap

Status: v1.0 · 2026-07-03 · Engineering phases are keyed to the GTM gates and kill criteria in `GROWTH-AGENT-SMB-Research-2026-07-03.md` §6–7. Rule: **no phase builds ahead of its gate.** Every phase ships something a client (or the operator) touches — infrastructure only rides along.

Operator constraint honored throughout: solo builder; fulfillment ≤2h/day once clients exist; build time is whatever remains.

---

## Phase A — Proof Engine (Jul 3 → Jul 18) · GATE-2 COMMITTED DATE

**Purpose:** the Gate-2 delta loop v1: 5 prompts × 3 engines × 3 runs, scheduled captures, variance verdicts, ≥3 treatment + 2–3 control domains. Also supports Gate 1's pre-registered panel (criteria locked **before Jul 7** — that's a document + thresholds file, do it first).

**Engineering deliverables (platform/, minimal viable slice):**
1. Repo scaffold: monorepo, `platform/` (FastAPI skeleton + worker), `registry/`, `ops/` (migrations, railway.json), CI (lint, tests, migration dry-run).
2. New dedicated Supabase project (Frankfurt, RLS ON). Tables only as needed: `orgs, sites (is_control flag), tracked_prompts (versioned/immutable), citation_runs (frozen panel), citation_results (sample_index, cited_url, engine_model_version), jobs (+lease columns), schedules, cost_events, quota_ledgers`.
3. Job runner v0: claim-then-work with lease + heartbeat + reaper; catch-up scheduler on a direct session-mode connection. (Small — but this is the spine everything else rides; do not fake it with cron.)
4. `CitationSamplerPort`: ChatGPT + Perplexity + Gemini API sampling; scheduled runs; raw responses to object storage with TTL.
5. Variance verdict calculator (pure Python): citation rate ± binomial CI per prompt; treatment-vs-control drift comparison against the pre-registered thresholds file; "named in 7/9 runs, was 1/9" formatting.
6. Per-domain lever log (`ops/levers.md` or a table) — Gate 1 requires knowing exactly what was changed where and when.
7. Operator CLI/console v0: run status, verdict export as a dated evidence log (client-forwardable text/HTML).

**Explicitly NOT in Phase A:** GSC, WordPress, content engine, briefs, brand profiles, any UI beyond the operator console, OAuth of any kind.

**Exit gate (Sep 1 verdict, panel runs from Jul 18):** treated domains beat control drift on ≥2 of 5 prompts under pre-registered thresholds. Muddy readout = fail. **Gate-1 null → the wedge is dead in both lanes; stop building.**

---

## Phase B — Autopsy Factory (Jul 18 → Aug 15)

**Purpose:** make the paid artifact (AED 1,850/location group autopsy) production-grade and cheap to fulfill, powering GTM Phase 2 (10 GCC group prospects, ≥1 paid autopsy + ≥6 group conversations by Oct 1).

**Engineering deliverables:**
1. Port the fable auditor into `platform/src/gm/audit`: deterministic scripts (BEV, robots, sitemap, schema), the 15-phase pipeline, `scoring.py` near-verbatim, `safety.py` wrapping every outbound socket.
2. `registry/` v1: transcribe the 103-check ruleset as data with per-check versions; merge the 55-check blog rubric IDs (unified namespace, content checks flagged); golden fixtures ported from both repos into CI; prompt-injection fixtures added.
3. Audits + findings tables per architecture §4; all grading in `audits`.
4. `delta.py` port + `content_deltas`/`site_deltas` with FK provenance.
5. Autopsy report renderer: share-token HTML (hashed tokens, 60d expiry, strict CSP, text-encoded evidence), print stylesheet, per-location sections + group rollup. No PDF build.
6. Sieve brain snapshot (12,764 entries) loaded into `registry/` as the citations store; deterministic citation ranker ported.
7. Minimal internal admin (2–3 days, founder-only): per-site job/audit timeline, finding-evidence viewer, dead-job retry, cost sums. (This is support tooling for "why is my score X" — cheaper now than during client delivery.)
8. Fetcher-worker posture: no credentials exist yet in the system — but the fetcher/publisher env split is scaffolded so Phase C lands into it.

**Parallel rail (starts now, zero code):** Google Cloud project + OAuth consent screen + verification paperwork (homepage, privacy policy, Limited Use answers, demo video). Weeks of lead time; needed for Phase E self-serve, NOT for concierge (service-account-added-as-property-user covers Phases C–D).

**Exit gate (GTM):** autopsy fulfillment ≤ half a day per group all-in. Aug 1 tripwire: <3 paid from ~100 verdicts → kill the paid-autopsy tier (build attention shifts to whatever the verdict siege converts).

---

## Phase C — Fulfillment Loop (Aug 15 → Oct 15)

**Purpose:** executed fixes + receipts — the concierge deliverable that separates this from every "recommendations" tool. Powers ≥5 cumulative paid cards by Oct 15 (the gate that decides whether the agency lane ever opens) and the 3 before/after case studies.

**Engineering deliverables:**
1. `connections` package + vault (sealed box, key_version, publisher-only private key, escrow + runbook). GSC via service account; WordPress via App Passwords with connect-time capability check + preflight (test draft round-trip, kses/schema survival, classified errors, export fallback).
2. Two-phase GSC ingest: aggregate 90d pull → provisional data in minutes; newest-first backfill throttled by quota ledger; slice-replacement ingest; trailing 3-day re-pull; gap semantics (no fake decay, receipts annotate gaps).
3. Three SQL detectors on rollups (striking-distance, decay, CTR-outlier) + GSC-based cannibalization second. `queue_items` with target_hash upsert + snooze. **Operator-facing** — this drives the 2h/day fulfillment queue, not a customer dashboard.
4. Fix execution: schema/JSON-LD generation with local validation; `content_item_findings` fix-claims; content fix-closer via **ContentEnginePort** — wrap serp-analyzer/blog-buster as `content-engine/` (authed internal service, budget tokens, per-round jobs). **Precondition: the convergence bug (drafts plateau 42–51, technical layer = 0) is diagnosed and fixed as the first task of the wrap — no fix-closer ships until the loop demonstrably converges on the unified registry.**
5. Publish + verify: WP draft-mode publish (pages upsert + page_id set in-transaction), IndexNow, sitemap resubmit, BEV re-probe + GSC URL Inspection at T+15m/T+72h.
6. Delta Receipt v1: FK-provenanced content_deltas + citation rate ± CI vs control + GSC before/after (final days only) → share-token HTML receipt. Brand profile build (brandsmith-lite) as fix-generation input.

**Exit gates (GTM):** Oct 1 — ≥1 paid group autopsy + ≥6 group conversations, else direct siege stops. Oct 15 — ≥5 paid cards or agency lane never opens; **<3 paid anything → fold Bet B entirely** (two-bet strategy).

---

## Phase D — Concierge Console & Channels (Oct 15 → Jan 15, 2027)

**Purpose:** retention machinery for converted concierge groups (≥1 group at AED 4,500+/mo) and the gated agency test (Proof Pack $500, 15 ranked SEO-led agencies, verdict Jan 15).

**Engineering deliverables:**
1. **WhatsApp booked-lead trend card** via Meta Cloud API (GrowthMonk WABA, already proven): weekly card to the group buyer — booked-consult trend + one delta highlight + one next action. Template approval lead time — submit templates early Nov.
2. Monthly receipt assembly job (site_deltas + citation panels + fix log) — auto-drafted, operator-approved before send.
3. Proof Pack generator (**gated — build only if the Oct 15 gate passed**): white-label-themed autopsy + fix instructions + variance delta; org-level theming on share pages only.
4. Reviews/reputation trust-signal check family added to `registry/` (diagnostic only — research CORE, this phase).
5. Refusal log for the agency pitch loop (a table + admin view — the >50% DIY-refusal early-death tripwire needs data, not memory).

**Exit gates (GTM):** Jan 15 — ≥2 agencies PAID or white-label dies (GCC-concierge-only). Concierge kill: month-3 renewal <50% or one refund → freeze group sales.

---

## Phase E — Productize the Winning Lane (Jan 15 → Mar 31, 2027)

**Purpose:** automate whichever lane passed to MRR ≥ $8–10k at ≤2h/day with fulfillment hours/client falling.

**Engineering deliverables (pull from the deferred list as the lane demands):** Stripe + plan states (past_due pauses paid-API jobs) · staged self-serve onboarding (needs the OAuth verification rail finished) · customer-facing queue + receipts UI · per-org budget enforcement · citation variance calibration study → repeat counts + de-beta the AI Visibility receipt section · embeddings features (internal links, clustering) · anti-slop QA hardening on the fix-closer · eval automation (nightly) · data study #2 published with raw logs.

**Kill criteria (standing):** Mar 31 — MRR <$5k or hours/client not falling → stop selling, run as cash engine, no third pivot. Capacity: >2h/day sustained 3+ weeks while Bet A has open deadlines → cut lowest-margin fulfillment. Integrity one-strike rule active from Phase A.

---

## Rails that run across all phases

| Rail | Cadence |
|---|---|
| Google OAuth verification (Phase E dependency) | Start Phase B, check monthly |
| Backups: Supabase PITR + weekly logical dump to object storage | From Phase A (the Sieve DB loss is the lesson) |
| Secrets inventory + rotation checklist in `ops/` | From Phase A; rotate quarterly |
| Golden + injection fixtures in CI before any prompt/model/registry change | From Phase B |
| Weekly hours ledger (operator) | From Phase A (GTM requirement) |
| Uptime ping + scheduler heartbeat alert | From Phase A |

## What this roadmap deliberately does not contain

Ad-spend-leak tooling (Phase-E-plus, manual, pull-based only) · monitoring dashboard as a product surface · landing-page/CRO/email/social modules · Reddit citation-seeding · any editor UI (WordPress-draft is the review surface) · staging environment · multi-region · public API. See docs/01-product.md §6 for the standing do-not-build list.
