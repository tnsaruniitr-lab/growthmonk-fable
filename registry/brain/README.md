# registry/brain — Sieve brain snapshots (evidence-citation data)

Verbatim copies of the Sieve brain export shipped with
`aeo-seo-auditor-fable/service/ruleset/` (website-seo-aeo-auditor v3 unified build).

- **Snapshots exported:** 2026-04-21 (rules 4,980 · anti-patterns 2,843 ·
  principles 3,728 · playbooks 1,213)
- **brain-mappings.json:** v1.4, last curated 2026-05-03 (108 check-id mappings
  into rules/anti-patterns, plus the source-tier definitions)
- **Copied into this repo:** 2026-07-03 — do NOT reformat or hand-edit; these
  files are treated as read-only data and are byte-identical to the source export.

Consumed by `platform/src/gm/audit/citations.py` (`load_brain` /
`rank_citations` / `attach_citations`). Mapping keys use unpadded check ids
(`A1_https_enforcement`); our registry uses zero-padded ids (`A-01`) — the
loader normalizes both sides, so keep both formats as-is.
