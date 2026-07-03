"""Evidence-citation layer — port of fable service/ruleset/ranker.py.

Every failed/warned finding gets up to three citations from the Sieve brain
snapshots (registry/brain/), selected by pure deterministic sorting — no LLM,
no network, same input → same output. The sort is the source ranker's exact
order: source tier ASC (tier 1 = Google/Schema.org official docs first), then
confidence DESC, then id ASC as the tiebreaker.

Data layout (registry/brain/):
    rules-snapshot.json, anti-patterns-snapshot.json, principles-snapshot.json,
    playbooks-snapshot.json  — JSON arrays of brain objects keyed by 'id'
    brain-mappings.json      — {"mappings": {check_id: {"rules": [...],
                               "anti_patterns": [...]}}, "source_tiers": {...}}

Check-id format drift: our registry uses zero-padded ids ("A-01") while the
mappings use unpadded suffixed keys ("A1_https_enforcement"). Both sides are
normalized to (letter, number) before lookup.

Adaptation vs the source ranker: candidates without a source_url are dropped —
a citation the client cannot follow is not evidence. page_type/industry tag
filtering is omitted (the snapshots carry no such tags; the source degraded to
a no-op on them anyway).

The snapshots total ~9 MB; load_brain() caches per-process — never load per
finding. Stdlib only; no DB, no network.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Source tiers (ordering per brain-mappings.json `source_tiers`; the alias
# sets below are the source ranker's operational encoding of those tiers,
# covering the source_org spellings that actually occur in the snapshots).
# ---------------------------------------------------------------------------

_TIER_1_SOURCES = frozenset({
    "Google", "Schema.org", "Perplexity", "Bing", "Microsoft",
    "W3C", "Apple", "Apple Developer", "Apple (developer.apple.com)",
    "OpenAI", "Anthropic", "Mozilla",
    "developers.google.com", "docs.perplexity.ai",
    "developer.mozilla.org", "schema.org",
})

_TIER_2_SOURCES = frozenset({
    "Backlinko", "backlinko.com", "Ahrefs", "Semrush",
    "Princeton", "arXiv", "Vercel", "BrightEdge",
    "Princeton/arXiv", "vercel.com",
})

_TIER_3_SOURCES = frozenset({
    "Search Engine Land", "Search Engine Journal", "Moz",
    "HubSpot", "blog.hubspot.com", "searchengineland.com",
    "searchenginejournal.com", "moz.com",
})

_TIER_4_SOURCES = frozenset({
    "amsive.com", "almcorp.com", "cxl.com",
    "seerinteractive.com", "Y Combinator", "apptweak",
    "Shopify", "Buffer", "frase.io", "animalz.co",
    "b2bcontentos.com", "appsflyer.com",
})

_DEFAULT_TIER = 5

# Maps the source_tiers section keys of brain-mappings.json to tier ranks so
# orgs curated there are honored even if missing from the static alias sets.
_MAPPINGS_TIER_KEYS = {
    "tier_1_primary": 1,
    "tier_2_research": 2,
    "tier_3_industry": 3,
    "tier_4_specialized": 4,
}

# Anti-patterns carry risk_level instead of confidence_score — same
# translation the source ranker used.
_RISK_CONFIDENCE = {"high": 0.95, "medium": 0.80, "low": 0.65}
_RISK_CONFIDENCE_DEFAULT = 0.75

# mapping-value key -> citation kind, and the snapshot field holding the title
_KIND_SPECS = (
    ("rules", "rule", "rules_by_id", "name"),
    ("anti_patterns", "anti_pattern", "aps_by_id", "title"),
    ("principles", "principle", "principles_by_id", "title"),
    ("playbooks", "playbook", "playbooks_by_id", "name"),
)

_CHECK_ID_RE = re.compile(r"^([A-Ja-j])[-_]?0*([0-9]+)")


class BrainError(ValueError):
    """Raised when the on-disk brain snapshot files are missing or malformed."""


def normalize_check_id(check_id: str) -> tuple[str, int] | None:
    """Collapse both id formats to (letter, number): 'A-01' and
    'A1_https_enforcement' both become ('A', 1). None when unparseable
    (e.g. the mappings' 'misc_*' keys)."""
    m = _CHECK_ID_RE.match(str(check_id).strip())
    if m is None:
        return None
    return m.group(1).upper(), int(m.group(2))


@dataclass
class Brain:
    """In-memory index over the brain snapshots. Build once per process."""

    rules_by_id: dict[int, dict] = field(default_factory=dict)
    aps_by_id: dict[int, dict] = field(default_factory=dict)
    principles_by_id: dict[int, dict] = field(default_factory=dict)
    playbooks_by_id: dict[int, dict] = field(default_factory=dict)
    mappings: dict[str, dict] = field(default_factory=dict)  # raw check_id -> mapping
    tier_overrides: dict[str, int] = field(default_factory=dict)  # from source_tiers
    # normalized (letter, number) -> mapping; derived in __post_init__
    mappings_normalized: dict[tuple[str, int], dict] = field(init=False)

    def __post_init__(self) -> None:
        normalized: dict[tuple[str, int], dict] = {}
        for key, mapping in self.mappings.items():
            norm = normalize_check_id(key)
            if norm is not None and norm not in normalized:
                normalized[norm] = mapping
        self.mappings_normalized = normalized

    def tier_of(self, source_org: str | None) -> int:
        if not source_org:
            return _DEFAULT_TIER
        override = self.tier_overrides.get(source_org)
        if override is not None:
            return override
        if source_org in _TIER_1_SOURCES:
            return 1
        if source_org in _TIER_2_SOURCES:
            return 2
        if source_org in _TIER_3_SOURCES:
            return 3
        if source_org in _TIER_4_SOURCES:
            return 4
        return _DEFAULT_TIER

    def mapping_for(self, check_id: str) -> dict | None:
        mapping = self.mappings.get(str(check_id).strip())
        if mapping is not None:
            return mapping
        norm = normalize_check_id(check_id)
        if norm is None:
            return None
        return self.mappings_normalized.get(norm)

    def stats(self) -> dict[str, int]:
        return {
            "rules": len(self.rules_by_id),
            "anti_patterns": len(self.aps_by_id),
            "principles": len(self.principles_by_id),
            "playbooks": len(self.playbooks_by_id),
            "mapped_checks": len(self.mappings),
        }


def _default_root() -> Path:
    # platform/src/gm/audit/citations.py -> repo root is parents[4]; registry/brain under it.
    return Path(__file__).resolve().parents[4] / "registry" / "brain"


def _load_array(root: Path, filename: str) -> dict[int, dict]:
    path = root / filename
    if not path.is_file():
        raise BrainError(f"brain snapshot not found: {path}")
    try:
        arr = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BrainError(f"{filename}: invalid JSON: {exc}") from exc
    if not isinstance(arr, list):
        raise BrainError(f"{filename}: expected a JSON array")
    return {entry["id"]: entry for entry in arr if isinstance(entry, dict) and "id" in entry}


_cache_lock = threading.Lock()
_brain_cache: dict[Path, Brain] = {}


def load_brain(root: Path | str | None = None) -> Brain:
    """Load the brain snapshots (~9 MB) with a per-process cache keyed by
    resolved root. First call per root pays the JSON parse; later calls are a
    dict hit. Raises BrainError when files are missing/malformed."""
    resolved = (Path(root) if root is not None else _default_root()).resolve()
    with _cache_lock:
        cached = _brain_cache.get(resolved)
        if cached is not None:
            return cached

    mappings_path = resolved / "brain-mappings.json"
    if not mappings_path.is_file():
        raise BrainError(f"brain mappings not found: {mappings_path}")
    try:
        mappings_doc = json.loads(mappings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BrainError(f"brain-mappings.json: invalid JSON: {exc}") from exc
    if not isinstance(mappings_doc, dict):
        raise BrainError("brain-mappings.json: expected a JSON object")

    tier_overrides: dict[str, int] = {}
    source_tiers = mappings_doc.get("source_tiers")
    if isinstance(source_tiers, dict):
        for key, rank in _MAPPINGS_TIER_KEYS.items():
            block = source_tiers.get(key)
            orgs = block.get("orgs") if isinstance(block, dict) else None
            for org in orgs or []:
                if isinstance(org, str):
                    tier_overrides.setdefault(org, rank)

    brain = Brain(
        rules_by_id=_load_array(resolved, "rules-snapshot.json"),
        aps_by_id=_load_array(resolved, "anti-patterns-snapshot.json"),
        principles_by_id=_load_array(resolved, "principles-snapshot.json"),
        playbooks_by_id=_load_array(resolved, "playbooks-snapshot.json"),
        mappings={
            str(k): v for k, v in (mappings_doc.get("mappings") or {}).items()
            if isinstance(v, dict)
        },
        tier_overrides=tier_overrides,
    )
    with _cache_lock:
        # Benign race: a concurrent loader may have beaten us; keep the winner.
        return _brain_cache.setdefault(resolved, brain)


def clear_brain_cache() -> None:
    """Drop cached brains (tests only — e.g. cold-load timing)."""
    with _cache_lock:
        _brain_cache.clear()


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


def _confidence_of(entry: dict, kind: str) -> float:
    if kind == "anti_pattern":
        return _RISK_CONFIDENCE.get(entry.get("risk_level"), _RISK_CONFIDENCE_DEFAULT)
    try:
        return float(entry.get("confidence_score", 0.5))
    except (TypeError, ValueError):
        return 0.5


def rank_citations(
    check_id: str,
    *,
    brain: Brain | None = None,
    mappings: dict[str, dict] | None = None,
    limit: int = 3,
) -> list[dict]:
    """Deterministic top-N citations for a check.

    Returns citation dicts {id, kind, title, source_org, source_url, tier,
    confidence} sorted tier ASC, confidence DESC, id ASC — the source ranker's
    exact order. Candidates lacking a source_url are dropped. Unknown or
    unmapped check ids return []. `mappings` overrides the brain's mapping
    table (keys in either id format)."""
    if brain is None:
        brain = load_brain()

    if mappings is not None:
        mapping = mappings.get(str(check_id).strip())
        if mapping is None:
            norm = normalize_check_id(check_id)
            if norm is not None:
                for key, value in mappings.items():
                    if normalize_check_id(key) == norm:
                        mapping = value
                        break
    else:
        mapping = brain.mapping_for(check_id)
    if not mapping:
        return []

    candidates: list[dict] = []
    for mapping_key, kind, index_attr, title_field in _KIND_SPECS:
        index: dict[int, dict] = getattr(brain, index_attr)
        for obj_id in mapping.get(mapping_key) or []:
            entry = index.get(obj_id)
            if entry is None:
                continue
            source_url = entry.get("source_url")
            if not source_url:
                continue
            candidates.append({
                "id": entry["id"],
                "kind": kind,
                "title": entry.get(title_field) or entry.get("title") or entry.get("name") or "",
                "source_org": entry.get("source_org"),
                "source_url": source_url,
                "tier": brain.tier_of(entry.get("source_org")),
                "confidence": _confidence_of(entry, kind),
            })

    candidates.sort(key=lambda c: (c["tier"], -c["confidence"], c["id"]))
    return candidates[:limit]


def attach_citations(
    findings: list[dict],
    statuses: set[str] = frozenset({"fail", "warn"}),
    *,
    brain: Brain | None = None,
    limit: int = 3,
) -> list[dict]:
    """Populate finding['citations'] in place for failed/warned checks that
    have mappings (top-`limit`); every other finding gets []. Returns the same
    list. Loads the brain once for the whole batch."""
    if brain is None and any(f.get("status") in statuses for f in findings):
        brain = load_brain()
    for finding in findings:
        if finding.get("status") in statuses:
            finding["citations"] = rank_citations(
                str(finding.get("check_id", "")), brain=brain, limit=limit,
            )
        else:
            finding["citations"] = []
    return findings
