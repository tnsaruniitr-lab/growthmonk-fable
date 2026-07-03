"""Check-registry loader (ADR-5, ADR-13).

The registry is DATA, not code: `registry/manifest.json` plus one
`registry/checks/<letter>.json` file per category letter (a.json … j.json),
each a JSON array of check objects in the format fixed by
docs/phase-b-contracts.md. Every audit pins the manifest version, and delta
comparability is decided per check_version — so the loader validates hard and
raises on any structural violation rather than limping along with a partial
ruleset.

Stdlib only; no DB, no network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

CATEGORY_LETTERS = frozenset("ABCDEFGHIJ")

VALID_BADGES = frozenset({
    "hard_evidence", "measured", "static_rule", "comparative", "heuristic", "model_judgment",
})

VALID_FIX_TYPES = frozenset({
    "page_html", "schema", "content_restructure", "sitewide_template",
    "cms_constraint", "offpage_entity", "cannot_fix_from_page",
})

_LEADING_LETTER = re.compile(r"^([A-Za-z])")


class RegistryError(ValueError):
    """Raised when the on-disk registry violates the Phase B format contract."""


@dataclass
class Registry:
    version: str
    checks: dict[str, dict] = field(default_factory=dict)  # check_id -> check object

    def category_of(self, check_id: str) -> str | None:
        """Category letter for a check_id — registry field first, then the
        leading letter of the id as a fallback for ids not in this registry."""
        check = self.checks.get(check_id)
        if check is not None:
            cat = str(check.get("category") or "").strip().upper()
            if cat and cat[0] in CATEGORY_LETTERS:
                return cat[0]
        m = _LEADING_LETTER.match(str(check_id).strip())
        if m and m.group(1).upper() in CATEGORY_LETTERS:
            return m.group(1).upper()
        return None

    def weight_of(self, check_id: str, default: float = 1.0) -> float:
        check = self.checks.get(check_id)
        if check is None:
            return default
        w = check.get("weight")
        if isinstance(w, bool) or not isinstance(w, (int, float)) or w <= 0:
            return default
        return float(w)


def _default_root() -> Path:
    # platform/src/gm/audit/registry.py -> repo root is parents[4]; registry/ under it.
    return Path(__file__).resolve().parents[4] / "registry"


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise RegistryError(msg)


def _validate_check(check: object, letter: str, fname: str, seen: set[str]) -> tuple[str, dict]:
    _require(isinstance(check, dict), f"{fname}: check entry is not an object")
    assert isinstance(check, dict)  # narrow for type checkers

    check_id = str(check.get("check_id") or "").strip()
    _require(bool(check_id), f"{fname}: check with missing/empty check_id")
    _require(check_id not in seen, f"duplicate check_id {check_id!r} (in {fname})")

    m = _LEADING_LETTER.match(check_id)
    _require(
        m is not None and m.group(1).upper() == letter,
        f"{fname}: check_id {check_id!r} does not start with category letter {letter!r}",
    )

    category = str(check.get("category") or "").strip().upper()
    _require(
        category == letter,
        f"{fname}: check {check_id!r} category {category!r} != filename letter {letter!r}",
    )

    version = check.get("check_version")
    _require(
        isinstance(version, int) and not isinstance(version, bool) and version >= 1,
        f"{fname}: check {check_id!r} has invalid check_version {version!r}",
    )

    badge = check.get("badge")
    _require(
        badge in VALID_BADGES,
        f"{fname}: check {check_id!r} has missing/unknown badge {badge!r}",
    )

    fix_type = check.get("fix_type")
    _require(
        fix_type in VALID_FIX_TYPES,
        f"{fname}: check {check_id!r} has missing/unknown fix_type {fix_type!r}",
    )

    weight = check.get("weight")
    _require(
        not isinstance(weight, bool) and isinstance(weight, (int, float)) and weight > 0,
        f"{fname}: check {check_id!r} has missing/non-positive weight {weight!r}",
    )

    return check_id, check


def load_registry(root: Path | None = None) -> Registry:
    """Load and validate the check registry.

    Validates: manifest has a version; every checks/<letter>.json is a JSON
    array whose category letters match the filename; check_ids are globally
    unique; every check carries check_version, badge, fix_type, and a positive
    weight. Raises RegistryError on any violation.
    """
    root = Path(root) if root is not None else _default_root()

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise RegistryError(f"registry manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry manifest is not valid JSON: {exc}") from exc
    _require(isinstance(manifest, dict), "registry manifest must be a JSON object")

    version = str(manifest.get("version") or manifest.get("registry_version") or "").strip()
    _require(bool(version), "registry manifest missing 'version'")

    checks_dir = root / "checks"
    if not checks_dir.is_dir():
        raise RegistryError(f"registry checks directory not found: {checks_dir}")

    files = sorted(checks_dir.glob("*.json"))
    _require(bool(files), f"no check files found under {checks_dir}")

    checks: dict[str, dict] = {}
    for path in files:
        letter = path.stem.upper()
        _require(
            len(path.stem) == 1 and letter in CATEGORY_LETTERS,
            f"unexpected registry file name {path.name!r} (want a.json … j.json)",
        )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RegistryError(f"{path.name}: invalid JSON: {exc}") from exc
        _require(isinstance(data, list), f"{path.name}: expected a JSON array of checks")
        for entry in data:
            check_id, check = _validate_check(entry, letter, path.name, set(checks))
            checks[check_id] = check

    return Registry(version=version, checks=checks)
