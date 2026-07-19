from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, event

SQLITE_BUSY_TIMEOUT_SECONDS = 5
SQLITE_BUSY_TIMEOUT_MS = SQLITE_BUSY_TIMEOUT_SECONDS * 1_000


def _configure_sqlite_connection(
    dbapi_connection: Any,
    _connection_record: Any,
) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def create_sqlite_aware_engine(database_url: str) -> Engine:
    is_sqlite = database_url.startswith("sqlite:")
    connect_args: dict[str, Any] = {}
    if is_sqlite:
        connect_args = {
            "check_same_thread": False,
            "timeout": SQLITE_BUSY_TIMEOUT_SECONDS,
        }
    engine = create_engine(database_url, connect_args=connect_args)
    if is_sqlite:
        event.listen(engine, "connect", _configure_sqlite_connection)
    return engine
