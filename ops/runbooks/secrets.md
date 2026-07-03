# Secrets inventory (Phase A)

| Secret | Mounted where | Rotation | Blast radius |
|---|---|---|---|
| DATABASE_URL | operator machine / worker | Supabase dashboard reset | full Phase A dataset |
| OPENAI_API_KEY | worker (sampler) | platform console; env swap | LLM spend only (read-only key) |
| PERPLEXITY_API_KEY | worker (sampler) | platform console; env swap | LLM spend only |
| GEMINI_API_KEY | worker (sampler) | AI Studio; env swap | LLM spend only |

Rules: env vars only — never committed, never logged (structlog scrubbers arrive Phase B);
`.env` is gitignored; rotate quarterly or on any suspicion. Vault (customer credentials)
does not exist until Phase C — this table grows before any customer credential is stored.
