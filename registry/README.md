# Check registry (data, not code)

`manifest.json` + `checks/{a..j}.json` — the versioned check registry (ADR-5, ADR-13).
Every audit pins the manifest version; delta comparability is per check_version.
Extracted from the aeo-seo-auditor ruleset v1.3 (103 checks). Format spec:
docs/phase-b-contracts.md.

Data caveats (verified 2026-07-04):

- **C-13 does not exist** — category C runs C-01..C-12 then C-14 (13 entries; the
  manifest count is correct). The id was skipped in the source ruleset, not lost in
  transcription. Do not "fix" the gap: audits/deltas key on check ids, and renumbering
  breaks comparability. New C checks take C-15+.
- **`brain/brain-mappings.json` carries 108 check-id mappings vs 103 registry checks**
  — the mappings were extracted from the v3 auditor, a different vintage than the v1.3
  ruleset, so up to 5 mapped ids have no corresponding check here. Dangling ids are
  harmless (lookups are check-id → brain entries, never enumerated in reverse). Its
  usage notes may also reference the retired live Sieve DB — the operative store is the
  frozen snapshot in `brain/`, loaded read-only by `gm/audit/citations.py`.
