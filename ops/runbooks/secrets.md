# Secrets inventory

| Secret | Mounted where | Rotation | Blast radius |
|---|---|---|---|
| DATABASE_URL | operator machine / worker / api / backup | Railway Postgres reset | full dataset |
| ADMIN_TOKEN | api | new random 32 hex; env swap | /admin/* read + job retry |
| OPENAI_API_KEY | worker (sampler) — **not yet set** | platform console; env swap | LLM spend only (read-only key) |
| PERPLEXITY_API_KEY | worker (sampler) — **not yet set** | platform console; env swap | LLM spend only |
| GEMINI_API_KEY | worker (sampler) — **not yet set** | AI Studio; env swap | LLM spend only |
| ANTHROPIC_API_KEY | worker (audit classifier) — **not yet set** | Anthropic console; env swap | LLM spend only |
| DATAFORSEO_LOGIN / _PASSWORD | worker / api | DataForSEO dashboard | SERP/Labs spend only |
| WABA_* ×4 (token, phone id, app secret, verify token) | api + worker — **not yet set** | Meta Business console | WhatsApp send + webhook auth |
| GM_VAULT_PRIVATE_KEY | publisher role only — NEVER on fetch-only workers | `gm vault keygen` + escrow runbook | every customer credential |
| GSC service-account JSON | vault (sealed) — **not yet created** | Google Cloud console | client Search Console read |
| Railway workspace token | operator keychain | Railway dashboard | whole Railway workspace |

Rules: env vars only — never committed, never logged; `.env` is gitignored; rotate
quarterly or on any suspicion.

**Rotations owed as of 2026-07-04** (all appeared in chat transcripts — HANDOFF §2):

| Credential | Where to rotate | Then update |
|---|---|---|
| Railway workspace token | Railway dashboard → workspace settings → tokens | operator keychain |
| Anthropic key (sieve-crawler) | Anthropic console → API keys | sieve-crawler env; gm-fable worker if reused |
| DATAFORSEO_PASSWORD | DataForSEO dashboard | Railway worker + api env |

Mark each done by moving it out of this table with the rotation date.
