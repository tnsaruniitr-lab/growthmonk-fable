# Convergence Diagnosis — Why Auto-Edit Runs Plateau at 42–51 (Technical = 0)

**Date:** 2026-07-03
**Scope:** serp-analyzer (Shakes-peer writer + auto-edit pipeline) x blog-buster (audit engine)
**Verdict up front:** This is NOT a writer-output vs auditor-expectation *shape* mismatch. The adapter handshake works. The plateau is caused by (1) every recorded run being launched **without editorial E-E-A-T inputs** (`enforce_human_signals: false`, no author, no first-party data), (2) blog-buster **folding unfixable eeat criticals into the TECHNICAL layer score** with a harsh linear penalty that clamps it to 0, and (3) those same criticals **aborting blog-buster's inner fix loop after iteration 1**, so most auto-fixable technical findings are exported without patch envelopes and never get fixed. One genuine contract mismatch exists on top (FAQ-count heuristic), worth ~14 points of permanent technical penalty.

---

## 1. Observed symptom (all 13 recorded runs)

Every `blogs/*/history.json` terminates `needs_review` with `technical: 0` (one run: 22) and overall 41–53:

| run | terminal | last score | technical | humanization | quality |
|---|---|---|---|---|---|
| answermonk-…-aeo | needs_review | 41 | 0 | 64 | 60 |
| answermonk-…-aeo-v2 | needs_review | 47 | 0 | 71 | 73 |
| tryps-best-group-trip-…-2026 | needs_review | 53 | **22** | 75 | 63 |
| tryps-oahu-… (5 variants) | needs_review | 46–51 | 0 | 66–83 | 67–80 |
| valeo-… (5 variants) | needs_review | 46–50 | 0 | 71–80 | 70–73 |

Excerpt — `blogs/valeo-health-q1-health-check-2026-wellness-goals-v5/history.json`:

```json
{
  "terminal": "needs_review",
  "terminalReason": "reached maxRounds=3 with 6 open item(s) for editorial review",
  "versions": [
    { "version": 2, "score": 42, "layerScores": { "technical": 0, "humanization": 62, "quality": 70, "overall": 42 }, "verdict": "block", "criticalCount": 2, "innerIterations": 1 },
    { "version": 5, "score": 48, "layerScores": { "technical": 0, "humanization": 76, "quality": 70, "overall": 48 }, "verdict": "block", "criticalCount": 2, "innerIterations": 1 }
  ],
  "openItems": [
    { "checkId": "E_author_sameas_missing", "severity": "critical",
      "evidence": "Author has no sameAs URLs (should link LinkedIn + at least one other profile)",
      "suggestedFields": ["author.linkedin_url"] },
    { "checkId": "E_human_signals_bundle_incomplete", "severity": "critical",
      "evidence": "Only 3/4 human signals present (author+LinkedIn=false, first-party data=false, ...)" },
    { "checkId": "E_no_first_party_data", "severity": "fail", "suggestedFields": ["first_party_data"] }
  ]
}
```

Note `innerIterations: 1` on **every** scored version, and the same two criticals persisting from `firstSeenVersion: 2` to the last round in every run.

## 2. Root-cause chain (with file:line evidence)

### 2.1 The runs were launched without editorial E-E-A-T data — deliberately

Every recorded run script disables human signals and supplies no `author`, `first_party_data`, or `named_examples`:

- `serp-analyzer/scripts/write-valeo-q1-health-v5.mjs:66` — `enforce_human_signals: false, // no real author/LinkedIn yet → will surface as open item`
- Same line (~49–89) in all 13 launch scripts (`write-tryps-*.mjs`, `write-answermonk-*.mjs`, `write-valeo-*.mjs`, `auto-edit-*.mjs`).

The writer therefore emits the fallback Person node with **no `sameAs`, no `jobTitle`** — `serp-analyzer/src/blog/writer.ts:1384-1390` (fallback branch: `{"@type":"Person","name","url","worksFor"}` only; the full branch at 1364–1383 with `sameAs: [linkedin_url, ...]` requires `input.author`). Confirmed in the recorded artifact `blogs/valeo-…-v5/v4/jsonld.json`: the Person entity has exactly `name`, `url`, `worksFor`.

### 2.2 Blog-buster flags 2 unfixable criticals every round

`blog-buster/src/layers/eeat/index.ts:121-134` — no `person.sameAs` → `E_author_sameas_missing` (critical). `:189-190` — LinkedIn-in-sameAs is one of the 4 "human signals", so `E_human_signals_bundle_incomplete` also fires (critical). Neither has a `suggestedPatch` — they require request-level data the loop cannot invent.

### 2.3 The criticals zero out the TECHNICAL layer score

`blog-buster/src/engine/scorer.ts:20-22`:

```ts
export function scoreTechnical(findings: Finding[]): number {
  return scoreLayer(findings, ["technical", "eeat"]);   // eeat folds into "technical"
}
```

with linear penalties (`scorer.ts:4-9`): critical −30, fail −14, warn −5, clamped at 0. In `blogs/valeo-…-v5/v4/findings.json` the technical+eeat bucket contains 2 criticals + 2 fails + 5 warns = **113 penalty points → max(0, 100−113) = 0**. The one run that scored 22 (tryps-group-apps) had 1 critical + 2 fails + 4 warns = 78 → 22. The arithmetic exactly reproduces every recorded technical score. There is no missing/renamed field, no directory-layout, no HTML-vs-JSON handover problem: the audit *sees* the writer's schemas, meta tags and body fine (findings reference the writer's actual FAQPage/Person entities and meta description text).

### 2.4 The same criticals ABORT blog-buster's inner fix loop after 1 iteration

`blog-buster/src/engine/loop.ts:156-167` — if any critical has no `suggestedPatch`, the loop stops. Confirmed in `blogs/valeo-…-v5/v4/audit.full.json`:

```
stopReason: "2 critical finding(s) have no auto-fix (E_author_sameas_missing,
             E_human_signals_bundle_incomplete) — human edit required"
status: escalated, iterations: 1
```

Consequence: blog-buster's own planners (e.g. `engine/planners/meta-planners.ts:108` regenerates `M_description_length`; schema planners for `D_entity_missing_id` / `D_Person_missing_recommended`) **never run**, so the exported `shakespeerInstructions` carry no patch envelope for those checks (`output/shakespeer-instructions.ts:93` only attaches a patch if `f.suggestedPatch` exists). Shakes-peer's dispatcher then records exactly what the traces show (`blogs/valeo-…-v5/v4/dispatch.json`):

```
M_description_length      | edit_schema     | skipped | no patch envelope
D_Person_missing_recommended | insert_missing | skipped | no patch envelope
D_entity_missing_id       | insert_missing  | skipped | no patch envelope
S_tldr_word_count         | attempt_rewrite | skipped | no patch/before to rewrite
```

So even the *auto-fixable* technical warns persist round after round. The two criticals also force `verdict: "block"` (`scorer.ts:57-66`), so the outer loop (`auto-edit-pipeline.ts`) can never reach `ship` and exits at `maxRounds` with `needs_review`.

### 2.5 Why overall is mathematically capped at ~48

`blog-buster/src/config.ts:10-14` — weights technical 0.35 / humanization 0.40 / quality 0.25. With technical = 0, **overall ≤ 0.4·H + 0.25·Q ≤ 65** even with perfect other layers; at the observed H≈76, Q≈70 → overall ≈ 48. The recorded 42–51 plateau is exactly this ceiling. The humanization/quality layers are **not** structurally broken by the same defect — they score in their normal band — but they are *indirectly* depressed: the inner-loop abort (2.4) means blog-buster's internal rewriter never iterates, and Shakes-peer's outer patches partially fail as `drift` ("before string not found", 1–3 per round), so humanization stalls at 62→83 instead of climbing.

### 2.6 One genuine contract mismatch: the FAQ parity heuristic (~14 pts, permanent)

`blog-buster/src/shared-lib/validators.ts:55-64` (`faqVisibleCount`) counts **every** question-shaped `h2/h3/h4/strong/summary/dt` in the whole document. The Shakes-peer writer intentionally writes AEO-style *question* H2 section headings **plus** a dedicated `<section id="faq">` with 7 `data-faq-item` blocks. Result: 7 questions in FAQPage schema vs "~14 visible FAQ pairs" → `P_faq_count_mismatch` (fail, −14) fires every round. Worse, Shakes-peer's own handler (`src/handlers/synthesize-content.ts:249`) counts only `data-faq-item` nodes, "rebuilds FAQPage from 7 visible FAQ(s)", reports `applied` — and the finding re-fires next round because the two sides define "visible FAQ" differently. This is the only true writer-shape vs auditor-expectation disagreement found, and it is worth 14 points, not 100.

## 3. Minimal fix plan (wrapper-side; no changes to either TS repo)

The GrowthMonk wrapper controls the `BlogWriterRequest` it submits to `generateAndAutoEdit()`. The fix is almost entirely input, not code:

1. **Always submit the full E-E-A-T bundle** (this alone removes both criticals, unblocks blog-buster's inner loop, restores planner patch envelopes, and lifts technical from 0 to ~70–90):
   - `enforce_human_signals: true`
   - `author: { name, title, bio (≥30 chars), linkedin_url (linkedin.com URL) }` — schema at `serp-analyzer/src/blog/types.ts:34-47`
   - `first_party_data: [≥1]`, `named_examples: [≥3]`, `editorial_stance`, `original_visuals: [≥1]` (superRefine at `types.ts:149-186` enforces these; treat them as required wrapper fields, sourced from the brand's Brandsmith record / onboarding form)
   - optionally `reviewer` for the reviewedBy schema.
   The Zod schema will hard-fail the request if the bundle is incomplete — that is the correct behavior: fail at intake, not at round 3 of a $0.75 loop.
2. **Wrapper-side FAQ-parity mitigation** (avoids the permanent −14 without touching either repo): in the wrapper's request, instruct the topic/angle so section headings are statements rather than questions is NOT reliable (writer prompt bakes in question H2s). Instead, accept the −14 in wave 2 and set the convergence bar accordingly (see §5), and file a follow-up against blog-buster to scope `faqVisibleCount` to `#faq` / `data-faq-item` containers (one-line selector change in `validators.ts:58`). That repo change is the only code fix worth making and it is 1 line + 1 test.
3. **Do not raise maxRounds** to chase 90 — the plateau was never a rounds problem; each extra round costs ~$0.19 and reconverges to the same ceiling.
4. Optional knobs already exposed if calibration is needed: `BLOG_AUDITOR_TARGET_SCORE` env (`blog-buster/src/config.ts:6`) and `targetScore`/`scoreWeights` in `AuditOptions` — prefer honest inputs over lowering the bar.

## 4. Will scores plausibly reach 85+ once fixed?

Projection from the recorded v4 findings with the E-E-A-T bundle supplied:

- Resolved outright: `E_author_sameas_missing` (−30), `E_human_signals_bundle_incomplete` (−30), `E_no_first_party_data` (−14), `E_author_credentials_missing` (−5), `D_Person_missing_recommended` (−5) → +84 penalty points returned.
- Newly fixable (inner loop no longer aborts; planners emit patches): `M_description_length`, `S_tldr_word_count`, `D_entity_missing_id` (−15 combined).
- Residual: `P_faq_count_mismatch` (−14) until the 1-line blog-buster scoping fix lands.

Technical: 0 → **~86** (or ~100 with the FAQ fix). Humanization: 76–83 observed with the inner rewriter disabled; with 5 inner iterations available, 85+ is realistic. Quality: 70–80 observed. Weighted: 86·0.35 + 83·0.40 + 75·0.25 ≈ **82**; with FAQ fix + inner-loop humanization gains: 100·0.35 + 88·0.40 + 78·0.25 ≈ **90**. So: **85+ is plausible after the input fix alone; 90 additionally requires the FAQ-heuristic scoping fix (or equivalent) and a working inner loop — not more outer rounds.** Also note: with zero criticals the verdict flips from `block` to `ship` at `overall ≥ targetScore`, so the loop can actually terminate `ship` for the first time.

## 5. Residual risks

- **FAQ parity −14** persists until blog-buster's `faqVisibleCount` is scoped (or the writer stops using question-style H2s — not recommended; it is deliberate AEO style and `S_h2_question_ratio_low` is a pending rule-authority item in `serp-analyzer/docs/handshake-contract.md:441`).
- **Drift patches**: 1–3 LLM-judge patches per round fail with "before string not found" (patch targets go stale after earlier patches mutate the HTML). Depresses humanization convergence speed; pre-existing, unrelated to the blocker.
- **Word-count band**: short posts trigger `S_word_count_below_band` (fail, −14) — wrapper should set `target_word_count ≥ 1400` for comparison-format posts (tryps run hit this at 1033 words).
- **Fabrication risk**: `first_party_data` / `named_examples` must be real brand data; the schema cannot verify truthfulness. Wrapper intake must source these from the client, not have an LLM invent them (this is also a stated design intent in the launch scripts' comments).
- **Judge nondeterminism**: humanization/quality are LLM-judged (claude models, `config.ts:8-9`); ±5 points run-to-run variance observed across the 5 valeo variants. Verification should use score bands, not exact values.

## 6. Verification recipe for wave 2

1. **Fixture**: reuse the valeo Q1-health request (`serp-analyzer/scripts/write-valeo-q1-health-v5.mjs`) but with `enforce_human_signals: true` and a complete bundle: real-shaped author (`name/title/bio/linkedin_url`), 1 `first_party_data`, 3 `named_examples`, 1 `editorial_stance`, 1 `original_visuals`, `target_word_count: 1800`.
2. **Run**: `generateAndAutoEdit(request, { maxRounds: 3, targetScore: 90, runLlm: true, push: false })`.
3. **Assert (gate A — the blocker is dead)**: in the new `history.json`, every scored version has `criticalCount: 0`, `technical ≥ 70`, and `innerIterations ≥ 2` (proves the inner loop no longer aborts at iteration 1); no `openItems` with checkId `E_author_sameas_missing` / `E_human_signals_bundle_incomplete` / `E_no_first_party_data`.
4. **Assert (gate B — convergence)**: final overall **≥ 80 within 3 rounds** while the FAQ heuristic mismatch stands (expected residual −14 on technical + judge variance); raise the gate to **≥ 90** only after the blog-buster `faqVisibleCount` scoping fix ships. Terminal must be `ship` or, if `needs_review`, the only open items must be non-eeat.
5. **Cost guard**: total run cost ≤ $1.00 (observed ~$0.19/round + verification audit).

## 7. Answers to the specific questions asked

- **Why does technical score 0?** Not missing/renamed fields, not directory layout, not HTML-vs-JSON, not a version mismatch. `scoreTechnical` = technical **+ eeat** findings on a 100-point linear scale; runs launched without author/first-party data guarantee 2 eeat criticals (+60) plus fails/warns → penalty >100 → clamp to 0. Handover (`blog-buster-adapter.ts` → `loadFromPostObject`) is shape-correct end to end.
- **What artifact does blog-buster audit?** An in-memory `BloggerPost` (`blog-buster/src/input/from-post-object.ts:5-17`) — `html`, `articleBodyHtml`, `jsonLdSchemas[]`, `metaTags{}`, keywords — built by `toBloggerPost()` (`serp-analyzer/src/blog/blog-buster-adapter.ts:41-60`). No disk handoff; `outputDir` is only where blog-buster writes reports.
- **Are humanization/quality depressed by the same defect?** Partially: their detectors run fine, but the criticals abort blog-buster's inner rewrite loop (iterations always = 1), so they plateau lower and slower than they would otherwise. They are not zeroed.
- **85+ plausible?** Yes, from input fix alone (projection ~82–86 immediately, 85+ with inner-loop gains); 90 needs the 1-line FAQ heuristic fix in blog-buster as well.
