# GrowthMonk Fable

**Verified AEO Fix Fulfillment with delta receipts** — one engine: audit → executed fixes → dated, variance-aware re-score vs untouched control domains → client-forwardable proof.

Sold direct as GCC multi-location clinic-group concierge (primary lane) and white-labeled to SEO-led agencies (gated test lane, opens only after ≥5 direct paid cards).

> **This is not a "growth agent with 5 subagents."** The 2026-07-03 research verdict (22 agents, 2 workflows, fresh 2,001-thread Reddit crawl): one wedge (the AEO fix loop), one spine (variance-aware receipts), everything else checkbox or skip. See [docs/01-product.md](docs/01-product.md) for the full module verdicts.

## Status

**Phases A–D1 built** (2026-07-04, CI green: 685 tests vs Postgres 16, zero skips). Proof engine (3-engine citation sampling, Wilson-CI verdicts), 103-check deterministic audit + group autopsy + share reports, GSC ingest + opportunity detectors, fix-closer + WordPress publish/verify, Delta Receipt v1, rank/AI-Overview tracking, keyword gap, WhatsApp booked-lead capture + weekly trend card. Prod: Railway (`worker` + `api` + Postgres), push-to-`main` deploys. **Start with [docs/HANDOFF.md](docs/HANDOFF.md)** — the done-vs-remaining map; the immediate operator item is **lock `ops/gate1-thresholds.yaml` + seed the Gate-1 panel** (`ops/runbooks/gate1-lock-and-seed.md`).

Quickstart (no Docker; see HANDOFF §0 for the local pg16 recipe): `python3.12 -m venv .venv && .venv/bin/pip install -e "./platform[dev]" && cp platform/.env.example platform/.env` then `gm db migrate` and the flow in `platform/src/gm/cli.py`'s docstring. Full suite: `ruff check platform && pytest platform/tests -q`.

## Documentation

| Doc | Contents |
|---|---|
| [docs/01-product.md](docs/01-product.md) | Wedge, target segment, module verdicts (what passed research and what didn't), customer outputs by phase, do-not-build list |
| [docs/02-architecture.md](docs/02-architecture.md) | Platform architecture — adversarially reviewed (4 reviewers, 51 findings folded in): tenancy, jobs, data model, ports, security, scaling seams |
| [docs/03-roadmap.md](docs/03-roadmap.md) | Phased engineering + product plan keyed to the GTM gates and kill criteria |

## Repo layout

```
growthmonk-fable/
├── docs/                # product, architecture, roadmap, HANDOFF, phase contracts
├── platform/            # Python 3.12: FastAPI API + worker
│   └── src/gm/          # packages: core, connections, audit, intel, content, delivery, infra
├── registry/            # versioned check registry + citations brain (data, not code)
└── ops/                 # migrations, runbooks, evidence, receipts, briefs
```

The content engine (serp-analyzer/blog-buster wrap) runs as an external service on the
operator machine via `CONTENT_ENGINE_URL` — Railway deploy deliberately deferred
(vendoring decision, see HANDOFF §2 known debt). There is no `content-engine/` directory
in this repo.

## Source assets (existing repos this build lifts from)

| Asset | What it contributes | Where it lands |
|---|---|---|
| `aeo-seo-auditor-fable` | 103-check ruleset, deterministic `scoring.py`, `delta.py`, BEV probe, `safety.py` SSRF, report persistence | `platform/src/gm/audit`, `registry/` |
| `serp-analyzer` (Shakes-peer + 102-check auditor) | Structured draft writer, revision loop, brief schema | `content-engine/` |
| `blog-buster` | On-page audit engine, fix-plan taxonomy, regression lineage | `content-engine/` |
| `brandsmith` | Brand profile pipeline (crawl → research → synthesize → verify) | `platform/src/gm/content` (Phase C) |
| Sieve brain snapshot (12,764 entries in fable `service/ruleset/`) | Evidence citations for findings | `registry/` |
| GrowthMonk WABA (Meta Cloud API, proven) | WhatsApp receipt/trend-card delivery | `platform/src/gm/delivery` (Phase D) |
