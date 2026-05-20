from __future__ import annotations

import importlib
from threading import RLock
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.database import (
    resolve_database_url,
    resolve_sqlite_url_path,
    resolve_store_sqlite_path,
)
from db.base import Base
from db.migrations import apply_schema_migrations
from db.seed import seed_reference_data


def _build_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, future=True, pool_pre_ping=True, connect_args=connect_args)


def _resolve_runtime_database_url() -> str:
    database_url = resolve_database_url()
    if database_url.startswith("sqlite"):
        return f"sqlite:///{resolve_store_sqlite_path().as_posix()}"
    return database_url


DATABASE_URL = _resolve_runtime_database_url()
engine = _build_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session)
_initialized = False
_init_lock = RLock()


def _load_model_modules() -> None:
    for module_name in ("db.models.parser", "db.models.store"):
        importlib.import_module(module_name)


def init_db() -> None:
    global _initialized

    if _initialized:
        return

    with _init_lock:
        if _initialized:
            return

        sqlite_path = resolve_sqlite_url_path(DATABASE_URL)
        if sqlite_path is not None:
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)

        _load_model_modules()

        Base.metadata.create_all(bind=engine)
        apply_schema_migrations(engine, DATABASE_URL)
        with SessionLocal() as session:
            seed_reference_data(session)
        _initialized = True


def get_db_session() -> Generator[Session, None, None]:
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
