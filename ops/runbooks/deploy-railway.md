# Railway deploy (worker)

One-time:
1. Create the production Supabase project (Frankfurt / eu-central), run
   `DATABASE_URL=<supabase-direct-url> gm db migrate` from your machine
   (use the DIRECT connection string on port 5432, not the transaction pooler —
   the scheduler's advisory lock needs a session-mode connection).
2. Railway: new project `growthmonk-fable` → service `worker` from this GitHub repo.
   Builds from the root Dockerfile automatically.
3. Service variables: DATABASE_URL (direct URL), OPENAI_API_KEY, PERPLEXITY_API_KEY,
   GEMINI_API_KEY, RAW_STORE_DIR=/data/raw. Attach a volume at /data for raw samples.
4. Deploy. Logs should show `worker up (handlers: sample_citations, scheduled_run)`.

Then seed from your machine against the same DATABASE_URL:
`gm org create …` → `gm site add …` (+ `--control` sites) → `gm prompt add …`
→ `gm schedule add <domain>` → verify with `gm status` / `gm run list <domain>`.

Redeploy behavior: leased jobs survive redeploys — a killed worker's jobs are
reaped and requeued after lease expiry (120s default). Sampling runs resume where
they stopped; no duplicate spend (existing sample_indexes are skipped).
