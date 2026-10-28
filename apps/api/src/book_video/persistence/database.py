"""Small SQLite unit-of-work with reproducible SQL migrations."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Iterator


class Database:
    def __init__(self, path: str | Path | None = None) -> None:
        configured_path = (
            path
            or os.getenv("BOOK_VIDEO_DATABASE_PATH")
            or (Path.cwd() / ".local" / "book_video.sqlite3")
        )
        self.path = str(configured_path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            timeout=30,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self) -> None:
        migration_dir = Path(__file__).resolve().parents[3] / "migrations"
        with self._lock:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                row[0]
                for row in self._connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for migration in sorted(migration_dir.glob("*.sql")):
                if migration.stem in applied:
                    continue
                self._connection.executescript(migration.read_text(encoding="utf-8"))
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                    (migration.stem,),
                )
            self._connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()
