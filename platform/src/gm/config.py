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
    # Editable installs resolve __file__ inside the repo (parents[3] = repo root).
    # A pip-installed package resolves into site-packages, where parents[3] points
    # nowhere useful — fall back to the container convention (WORKDIR /app carries
    # COPY'd ops/ + registry/). GM_REPO_ROOT overrides both.
    env_root = os.environ.get("GM_REPO_ROOT")
    if env_root:
        return Path(env_root)
    dev = Path(__file__).resolve().parents[3]
    if (dev / "ops" / "migrations").is_dir():
        return dev
    return Path.cwd()


def migrations_dir() -> Path:
    return repo_root() / "ops" / "migrations"
