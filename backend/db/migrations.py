from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.engine import Connection, Engine

Migration = Callable[[Connection], None]

SCHEMA_MIGRATIONS_TABLE = "schema_migrations"
_SQLITE_TABLE_INFO_SQL = {
    "parser_models": "PRAGMA table_info(parser_models)",
    "kv_entries": "PRAGMA table_info(kv_entries)",
}


def apply_schema_migrations(engine: Engine, database_url: str) -> None:
    """Apply lightweight SQLite migrations for existing local deployments.

    Fresh databases are created from SQLAlchemy models first, then these
    migrations mark the known schema changes as applied. Existing SQLite
    databases get the missing columns added without keeping ad hoc ALTER
    statements in the session bootstrap code.
    """
    if not database_url.startswith("sqlite"):
        return

    with engine.begin() as conn:
        _ensure_schema_migrations_table(conn)
        _run_migration(
            conn,
            "20260512_001_parser_model_execution_backends",
            _migrate_parser_model_execution_backends,
        )
        _run_migration(
            conn,
            "20260512_002_kv_entries_updated_at",
            _migrate_kv_entries_updated_at,
        )


def _ensure_schema_migrations_table(conn: Connection) -> None:
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version VARCHAR(64) PRIMARY KEY,
            applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _run_migration(conn: Connection, version: str, migration: Migration) -> None:
    if _migration_applied(conn, version):
        return

    migration(conn)
    conn.exec_driver_sql(
        "INSERT INTO schema_migrations (version) VALUES (?)",
        (version,),
    )


def _migration_applied(conn: Connection, version: str) -> bool:
    row = conn.exec_driver_sql(
        "SELECT version FROM schema_migrations WHERE version = ?",
        (version,),
    ).first()
    return row is not None


def _sqlite_columns(conn: Connection, table_name: str) -> set[str]:
    table_info_sql = _SQLITE_TABLE_INFO_SQL[table_name]
    return {row[1] for row in conn.exec_driver_sql(table_info_sql).fetchall()}


def _migrate_parser_model_execution_backends(conn: Connection) -> None:
    columns = _sqlite_columns(conn, "parser_models")
    if not columns:
        return

    if "default_execution_backend" not in columns:
        conn.exec_driver_sql(
            "ALTER TABLE parser_models "
            "ADD COLUMN default_execution_backend VARCHAR(80) NOT NULL DEFAULT 'openai'"
        )

    if "supported_execution_backends_json" not in columns:
        conn.exec_driver_sql(
            "ALTER TABLE parser_models "
            "ADD COLUMN supported_execution_backends_json JSON NOT NULL DEFAULT '[]'"
        )


def _migrate_kv_entries_updated_at(conn: Connection) -> None:
    columns = _sqlite_columns(conn, "kv_entries")
    if not columns:
        return

    if "updated_at" not in columns:
        conn.exec_driver_sql(
            "ALTER TABLE kv_entries "
            "ADD COLUMN updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
        )
