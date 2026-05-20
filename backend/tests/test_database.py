from pathlib import Path

from core.database import (
    DEFAULT_DATABASE_URL,
    DEFAULT_DB_PATH,
    resolve_database_url,
    resolve_primary_sqlite_path,
    resolve_store_sqlite_path,
)


def test_resolve_database_url_uses_default_when_env_missing() -> None:
    assert resolve_database_url({}) == DEFAULT_DATABASE_URL


def test_resolve_primary_sqlite_path_reads_database_url() -> None:
    env = {"DATABASE_URL": "sqlite:///./data/custom.db"}

    assert resolve_primary_sqlite_path(env) == Path("data/custom.db").resolve()


def test_resolve_primary_sqlite_path_ignores_non_sqlite_database_url() -> None:
    env = {"DATABASE_URL": "postgresql://user:pass@localhost:5432/app"}

    assert resolve_primary_sqlite_path(env) is None


def test_resolve_store_sqlite_path_prefers_explicit_store_path() -> None:
    env = {
        "DATABASE_URL": "sqlite:///./data/from-database-url.db",
        "STORE_SQLITE_PATH": "./data/from-store-path.db",
    }

    assert resolve_store_sqlite_path(env) == Path("data/from-store-path.db").resolve()


def test_resolve_store_sqlite_path_falls_back_to_database_url_sqlite_path() -> None:
    env = {"DATABASE_URL": "sqlite:///./data/from-database-url.db"}

    assert resolve_store_sqlite_path(env) == Path("data/from-database-url.db").resolve()


def test_resolve_store_sqlite_path_falls_back_to_default_db_path() -> None:
    env = {"DATABASE_URL": "postgresql://user:pass@localhost:5432/app"}

    assert resolve_store_sqlite_path(env) == DEFAULT_DB_PATH.resolve()
