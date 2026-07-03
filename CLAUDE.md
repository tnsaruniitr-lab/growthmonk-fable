# growthmonk-fable — agent onboarding

**Start with [docs/HANDOFF.md](docs/HANDOFF.md)** — the done-vs-remaining map with live proof
for every claim, the ordered backlog, and the working pattern that built this repo.

Non-negotiables:
- LLMs classify/generate; **Python grades deterministically** (`gm/audit/scoring.py`). No score,
  grade, or delta is ever model-emitted.
- Honest failure everywhere: `inconclusive`/`na`/empty-state over guesses; never fake zeros;
  never invent facts, authors, or citations (grounding rules in `docs/phase-c-wave3-contracts.md`).
- Respect the GTM gates and the do-not-build list (`docs/01-product.md` §6, `docs/03-roadmap.md`).
- Contracts before code: extend `docs/phase-*-contracts.md`, keep file ownership disjoint.
- Migrations: take the next free number in `ops/migrations/` (collisions happened; check first).
- Verify: `.venv/bin/ruff check platform && pytest platform/tests -q` (local pg16 recipe in
  HANDOFF §0); CI must stay green (685 tests, zero skips as of 2026-07-04).

Production: Railway project `growthmonk-fable` (Postgres + worker + api; push-to-main deploys).
Reports served at `api-production-e922.up.railway.app/r/<token>`.
