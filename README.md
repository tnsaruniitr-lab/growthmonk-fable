# GrowthMonk Fable

**Verified AEO Fix Fulfillment with delta receipts** — one engine: audit → executed fixes → dated, variance-aware re-score vs untouched control domains → client-forwardable proof.

Sold direct as GCC multi-location clinic-group concierge (primary lane) and white-labeled to SEO-led agencies (gated test lane, opens only after ≥5 direct paid cards).

> **This is not a "growth agent with 5 subagents."** The 2026-07-03 research verdict (22 agents, 2 workflows, fresh 2,001-thread Reddit crawl): one wedge (the AEO fix loop), one spine (variance-aware receipts), everything else checkbox or skip. See [docs/01-product.md](docs/01-product.md) for the full module verdicts.

## Status

**Pre-code.** Repo scaffolded 2026-07-03 with the reviewed product + architecture + roadmap documentation. First committed engineering deliverable: **Gate-2 proof loop v1 by Jul 18, 2026** (see roadmap Phase A).

## Documentation

| Doc | Contents |
|---|---|
| [docs/01-product.md](docs/01-product.md) | Wedge, target segment, module verdicts (what passed research and what didn't), customer outputs by phase, do-not-build list |
| [docs/02-architecture.md](docs/02-architecture.md) | Platform architecture — adversarially reviewed (4 reviewers, 51 findings folded in): tenancy, jobs, data model, ports, security, scaling seams |
| [docs/03-roadmap.md](docs/03-roadmap.md) | Phased engineering + product plan keyed to the GTM gates and kill criteria |

## Planned repo layout

```
growthmonk-fable/
├── docs/                # product, architecture, roadmap (this set)
├── platform/            # Python 3.12: FastAPI API + worker fleet
│   └── src/gm/          # packages: core, connections, audit, intel, content, delivery, infra
├── content-engine/      # TS internal service (wrapped Shakes-peer writer + blog-buster loop) — Phase C
├── registry/            # versioned check registry (data, not code) + golden fixtures
└── ops/                 # migrations, railway config, runbooks (key-loss, incident, rotation)
```

## Source assets (existing repos this build lifts from)

| Asset | What it contributes | Where it lands |
|---|---|---|
| `aeo-seo-auditor-fable` | 103-check ruleset, deterministic `scoring.py`, `delta.py`, BEV probe, `safety.py` SSRF, report persistence | `platform/src/gm/audit`, `registry/` |
| `serp-analyzer` (Shakes-peer + 102-check auditor) | Structured draft writer, revision loop, brief schema | `content-engine/` |
| `blog-buster` | On-page audit engine, fix-plan taxonomy, regression lineage | `content-engine/` |
| `brandsmith` | Brand profile pipeline (crawl → research → synthesize → verify) | `platform/src/gm/content` (Phase C) |
| Sieve brain snapshot (12,764 entries in fable `service/ruleset/`) | Evidence citations for findings | `registry/` |
| GrowthMonk WABA (Meta Cloud API, proven) | WhatsApp receipt/trend-card delivery | `platform/src/gm/delivery` (Phase D) |
