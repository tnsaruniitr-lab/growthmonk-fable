# Gate-1: lock the pre-registration + seed the real panel

Written 2026-07-04. Deadline context: thresholds lock was committed for **before Jul 7**
(`ops/gate1-thresholds.yaml` header); panel runs must be producing weekly evidence from
**Jul 18** for the **Sep 1** verdict. The math has zero slack: 3-run baseline window +
3-run treatment window at 1 run/week = 6 weeks minimum, Jul 18 → Sep 1 is ~6.5 weeks.
Every idle week slips the verdict one-for-one. Gate-1 evidence is serial — it cannot be
backfilled later.

## Current state (verified live 2026-07-04)

- `ops/gate1-thresholds.yaml`: `status: DRAFT`, `locked_at: null`.
- Prod sites: `growthmonk.ai` (treatment), `smoke.growthmonk.ai` (smoke artifact —
  not a panel member). **No control sites. No tracked panel prompts. No schedules.**
- Prod worker env: `OPENAI_API_KEY` / `PERPLEXITY_API_KEY` / `GEMINI_API_KEY` **not set**
  — sampling jobs will fail until they are (see `ops/runbooks/secrets.md`).
- The only evidence artifact (2026-07-03 smoke) ran 1 prompt, no controls, verdict FAIL,
  under a DRAFT pre-registration — it is a plumbing proof, not Gate-1 evidence.

## Step 1 — lock (operator decision, ~2 minutes)

Review the thresholds. If accepted as-is, the lock is exactly this edit and nothing else:

```yaml
status: LOCKED
locked_at: <YYYY-MM-DD>   # the day you sign it
```

Commit with message `Gate-1 pre-registration LOCKED` and push. From that commit the file
is append-only: any change to panel/movement/gate values invalidates the pre-registration
(header rule). If a threshold looks wrong, change it NOW, before locking — not after.

## Step 2 — choose the panel (operator decision)

Per the locked panel spec: ≥3 treatment + 2–3 control domains, 5 prompts each.

- **Treatment** = domains you control and will apply logged levers to.
  `growthmonk.ai` is the obvious first member (already instrumented, already audited).
- **Control** = same-vertical domains you will NOT touch for the whole window.
  They exist to absorb engine drift; pick domains comparable in size/topic, not giants.
- **Prompts**: phrase them the way a buyer would ask an assistant (e.g. "best <service>
  in <city>", "<service> worth it?", "top tools for <job>") — not keyword strings.
  Prompts are versioned and immutable once tracked; wording changes = new prompt.

## Step 3 — seed (mechanical, once keys are set)

```sh
export DATABASE_URL=<prod direct URL>   # session-mode, port 5432
gm site add <treatment.domain> --brand-term "<Brand>"
gm site add <control.domain> --control
gm prompt add <domain> "<prompt text>"       # ×5 per site
gm lever add <treatment.domain> --class <onsite_fix|directory_listing|schema|content> \
    --description "<what changed>"           # BEFORE applying any change, every time
gm schedule add <domain> --every-minutes 10080   # weekly sampling per site
gm status                                        # confirm panel + schedules
```

Baseline discipline: **no levers during the 3-run baseline window.** The first lever on a
treatment domain starts its treatment clock; log it with `gm lever add` in the same sitting
as the change itself. Unlogged levers taint attribution (claim ceiling drops to "movement
vs control, lever unattributed").

## Step 4 — verify the cadence is real

After the first scheduled run completes: `gm run list <domain>` shows the run,
`gm verdict <domain> --before … --after … --controls …` renders (it will say
insufficient windows — expected until week 6). Check the worker logs on Railway for
`sample_citations` job completions; a run with `error`-marked samples on every row means
an engine key is missing/invalid — fix the env, the scheduler catches up automatically.
