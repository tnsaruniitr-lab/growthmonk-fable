"""Environment-backed settings. No config framework — one function, explicit names."""

import os
from pathlib import Path


def env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def database_url() -> str:
    return env("DATABASE_URL")


def raw_store_dir() -> Path:
    p = Path(os.environ.get("RAW_STORE_DIR", "./raw"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def repo_root() -> Path:
    # .../growthmonk-fable/platform/src/gm/config.py -> parents[3] = repo root
    return Path(__file__).resolve().parents[3]


def migrations_dir() -> Path:
    return repo_root() / "ops" / "migrations"
