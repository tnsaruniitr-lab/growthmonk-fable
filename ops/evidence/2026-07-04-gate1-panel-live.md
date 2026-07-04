# Gate-1 panel seeded + first baseline run — LIVE (2026-07-04)

Pre-registration **LOCKED 2026-07-04** (commit 25a9cb5; deadline was Jul 7). Panel seeded
and the first weekly runs completed the same day. The Sep 1 verdict clock is running.

## Panel (as seeded in prod)

- **Treatment**: growthmonk.ai (brand "GrowthMonk") — the only domain we control; the
  treatment panel grows as client domains join (documented deviation from the ≥3 target,
  honest: better one real treatment domain than invented ones).
- **Controls** (same vertical — website/SEO audit tools — comparable size, never touched):
  seoptimer.com, sitechecker.pro, seobility.net.
- 5 buyer-phrased prompts per site (20 total), engines openai+perplexity+gemini,
  weekly `scheduled_run` per site (samples_per_run=3).

## First runs — all 4 done (2026-07-04 ~15:35 UTC)

Each run: **15 ok samples (openai, live) + 30 error samples** — perplexity/gemini record
"adapter unavailable (missing API key?)" honestly; errors are excluded from rates per the
locked verdict rules. With openai alone, a 3-run window yields exactly the pre-registered
minimum of 9 samples/prompt. Adding PERPLEXITY_API_KEY / GEMINI_API_KEY strengthens the
panel but does not block it.

Baseline citation rates, run 1 (openai): growthmonk.ai **0/3 cited, 0/3 mentioned on all
5 prompts** — the honest low baseline the movement thresholds need headroom against.
Controls: all 0/3 cited; one mention (seobility.net, "free seo audit tool", 1/3).

## Audit engine e2e (ANTHROPIC_API_KEY live)

`gm audit growthmonk.ai` → job 14 on the prod worker → **done: C+ (71.8), $0.58** —
consistent with the 2026-07-03 handoff run (C+ 71.5, $0.59).

## Cadence discipline from here

- Baseline window: runs of Jul 4, ~Jul 11, ~Jul 18 (3 runs, no levers before the window
  closes). First lever on growthmonk.ai: log with `gm lever add` BEFORE applying, then
  treatment window = the following 3 weekly runs → verdict material well before Sep 1.
- Keys owed for full-panel strength: PERPLEXITY_API_KEY, GEMINI_API_KEY.
- Worker keys were shared in chat (rotation owed — ops/runbooks/secrets.md).
