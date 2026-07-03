# Check registry (data, not code)

`manifest.json` + `checks/{a..j}.json` — the versioned check registry (ADR-5, ADR-13).
Every audit pins the manifest version; delta comparability is per check_version.
Extracted from the aeo-seo-auditor ruleset v1.3 (103 checks). Format spec:
docs/phase-b-contracts.md.
