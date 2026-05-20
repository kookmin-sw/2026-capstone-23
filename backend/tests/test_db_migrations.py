from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from db.migrations import apply_schema_migrations


def _columns(conn: Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()}


def test_sqlite_schema_migrations_add_missing_columns_and_track_versions(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)

    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE parser_models (
                model_id VARCHAR(64) PRIMARY KEY,
                model_code VARCHAR(120) NOT NULL UNIQUE,
                display_name VARCHAR(120) NOT NULL,
                provider VARCHAR(80) NOT NULL,
                is_active BOOLEAN NOT NULL,
                config_json JSON NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE kv_entries (
                namespace VARCHAR(120) NOT NULL,
                entry_key VARCHAR(255) NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (namespace, entry_key)
            )
            """
        )

    apply_schema_migrations(engine, f"sqlite:///{db_path.as_posix()}")
    apply_schema_migrations(engine, f"sqlite:///{db_path.as_posix()}")

    with engine.begin() as conn:
        assert {"default_execution_backend", "supported_execution_backends_json"} <= _columns(
            conn,
            "parser_models",
        )
        assert "updated_at" in _columns(conn, "kv_entries")

        versions = [
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]

    assert versions == [
        "20260512_001_parser_model_execution_backends",
        "20260512_002_kv_entries_updated_at",
    ]
