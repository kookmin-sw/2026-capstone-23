from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any

from sqlalchemy import delete, func, select

from core.database import resolve_database_url, resolve_store_sqlite_path
from db.models import StoreEntry
from db.session import SessionLocal, init_db


_DB_LOCK = RLock()


def _resolve_store_path() -> Path:
    return resolve_store_sqlite_path()


def describe_store_target() -> str:
    database_url = resolve_database_url()
    if not database_url.startswith("sqlite"):
        return database_url
    return str(_resolve_store_path())


def load_json_entry(namespace: str, key: str) -> Any | None:
    init_db()
    with _DB_LOCK:
        with SessionLocal() as session:
            entry = session.get(StoreEntry, (namespace, key))
            if entry is None:
                return None
            return json.loads(entry.value)


def save_json_entry(namespace: str, key: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    init_db()
    with _DB_LOCK:
        with SessionLocal() as session:
            entry = session.get(StoreEntry, (namespace, key))
            if entry is None:
                session.add(StoreEntry(namespace=namespace, entry_key=key, value=payload))
            else:
                entry.value = payload
            session.commit()


def delete_entry(namespace: str, key: str) -> bool:
    init_db()
    with _DB_LOCK:
        with SessionLocal() as session:
            entry = session.get(StoreEntry, (namespace, key))
            if entry is None:
                return False
            session.delete(entry)
            session.commit()
            return True


def list_json_entries(namespace: str) -> list[tuple[str, Any]]:
    init_db()
    with _DB_LOCK:
        with SessionLocal() as session:
            rows = session.scalars(
                select(StoreEntry)
                .where(StoreEntry.namespace == namespace)
                .order_by(StoreEntry.entry_key)
            ).all()
            return [(row.entry_key, json.loads(row.value)) for row in rows]


def list_keys(namespace: str) -> list[str]:
    init_db()
    with _DB_LOCK:
        with SessionLocal() as session:
            return list(
                session.scalars(
                    select(StoreEntry.entry_key)
                    .where(StoreEntry.namespace == namespace)
                    .order_by(StoreEntry.entry_key)
                ).all()
            )


def count_entries(namespace: str) -> int:
    init_db()
    with _DB_LOCK:
        with SessionLocal() as session:
            count = session.scalar(
                select(func.count())
                .select_from(StoreEntry)
                .where(StoreEntry.namespace == namespace)
            )
            return int(count or 0)


def clear_namespace(namespace: str) -> None:
    init_db()
    with _DB_LOCK:
        with SessionLocal() as session:
            session.execute(delete(StoreEntry).where(StoreEntry.namespace == namespace))
            session.commit()
