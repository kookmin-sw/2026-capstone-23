from __future__ import annotations

from pathlib import Path
from typing import Mapping

from core.env import env_str


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "app.db"
DEFAULT_DATABASE_URL = f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"


def _env_value(name: str, env: Mapping[str, str] | None = None) -> str:
    if env is not None:
        return env.get(name, "").strip()
    return env_str(name, "", strip=True)


def resolve_database_url(env: Mapping[str, str] | None = None) -> str:
    database_url = _env_value("DATABASE_URL", env)
    return database_url or DEFAULT_DATABASE_URL


def resolve_sqlite_url_path(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite:///"):
        return None
    return Path(database_url.removeprefix("sqlite:///")).expanduser().resolve()


def resolve_primary_sqlite_path(env: Mapping[str, str] | None = None) -> Path | None:
    return resolve_sqlite_url_path(resolve_database_url(env))


def resolve_store_sqlite_path(env: Mapping[str, str] | None = None) -> Path:
    explicit_path = _env_value("STORE_SQLITE_PATH", env)
    if explicit_path:
        candidate = Path(explicit_path).expanduser()
        if not candidate.is_absolute():
            candidate = (PROJECT_ROOT / candidate).resolve()
        else:
            candidate = candidate.resolve()
        return candidate

    sqlite_path = resolve_primary_sqlite_path(env)
    if sqlite_path is not None:
        return sqlite_path

    return DEFAULT_DB_PATH.resolve()
