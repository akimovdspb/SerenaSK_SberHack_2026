from __future__ import annotations

import pathlib

from apps.api.app.sqlite_runtime import (
    SQLITE_BUSY_TIMEOUT_MS,
    create_sqlite_aware_engine,
)


def test_file_sqlite_engine_enforces_wal_timeout_and_foreign_keys(
    tmp_path: pathlib.Path,
) -> None:
    engine = create_sqlite_aware_engine(f"sqlite:///{tmp_path / 'factory.db'}")

    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar_one()

    assert journal_mode == "wal"
    assert busy_timeout == SQLITE_BUSY_TIMEOUT_MS
    assert foreign_keys == 1
    assert synchronous == 1
