"""SQLite-backed LangGraph checkpoints for worker restart recovery.

The saver uses only the Python standard library and stores LangGraph's typed
checkpoint payloads in a private table set. It is intentionally separate from
the domain schema: the integration layer owns where the SQLite file lives and
how long checkpoints are retained.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Mapping

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    CheckpointTuple,
    WRITES_IDX_MAP,
    get_checkpoint_id,
    get_checkpoint_metadata,
)


class SQLiteCheckpointSaver(BaseCheckpointSaver[str]):
    """Persist graph state and pending writes across saver instances/processes.

    ``path`` must point to a file-backed SQLite database. Each operation opens
    its own connection, so closing a worker does not leave a connection-owned
    lock behind and another process can resume the same ``thread_id``.
    """

    def __init__(self, path: str | Path, *, timeout_seconds: float = 30.0) -> None:
        super().__init__()
        self.path = Path(path)
        if str(path) == ":memory:":
            raise ValueError("SQLiteCheckpointSaver requires a file-backed database")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS langgraph_checkpoints (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    parent_checkpoint_id TEXT,
                    checkpoint_type TEXT NOT NULL,
                    checkpoint_data BLOB NOT NULL,
                    metadata_type TEXT NOT NULL,
                    metadata_data BLOB NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                );
                CREATE INDEX IF NOT EXISTS ix_langgraph_checkpoints_latest
                    ON langgraph_checkpoints(thread_id, checkpoint_ns, checkpoint_id DESC);
                CREATE TABLE IF NOT EXISTS langgraph_checkpoint_blobs (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    version_key TEXT NOT NULL,
                    value_type TEXT NOT NULL,
                    value_data BLOB NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, channel, version_key)
                );
                CREATE TABLE IF NOT EXISTS langgraph_checkpoint_writes (
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    write_index INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    value_type TEXT NOT NULL,
                    value_data BLOB NOT NULL,
                    task_path TEXT NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, write_index)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        connection.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
        return connection

    @staticmethod
    def _version_key(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    def _thread_config(self, config: Mapping[str, Any]) -> tuple[str, str]:
        configurable = config.get("configurable", {})
        thread_id = str(configurable.get("thread_id", ""))
        if not thread_id:
            raise ValueError("checkpoint config requires configurable.thread_id")
        return thread_id, str(configurable.get("checkpoint_ns", ""))

    def get_tuple(self, config: Any) -> CheckpointTuple | None:
        thread_id, checkpoint_ns = self._thread_config(config)
        requested_id = get_checkpoint_id(config)
        with self._connect() as connection:
            if requested_id:
                row = connection.execute(
                    "SELECT checkpoint_id, parent_checkpoint_id, checkpoint_type, checkpoint_data, "
                    "metadata_type, metadata_data FROM langgraph_checkpoints "
                    "WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                    (thread_id, checkpoint_ns, requested_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT checkpoint_id, parent_checkpoint_id, checkpoint_type, checkpoint_data, "
                    "metadata_type, metadata_data FROM langgraph_checkpoints "
                    "WHERE thread_id=? AND checkpoint_ns=? ORDER BY checkpoint_id DESC LIMIT 1",
                    (thread_id, checkpoint_ns),
                ).fetchone()
            if row is None:
                return None

            checkpoint_id, parent_id, checkpoint_type, checkpoint_data, metadata_type, metadata_data = row
            checkpoint = self.serde.loads_typed((checkpoint_type, checkpoint_data))
            channel_values: dict[str, Any] = {}
            for channel, version in checkpoint.get("channel_versions", {}).items():
                blob = connection.execute(
                    "SELECT value_type, value_data FROM langgraph_checkpoint_blobs "
                    "WHERE thread_id=? AND checkpoint_ns=? AND channel=? AND version_key=?",
                    (thread_id, checkpoint_ns, channel, self._version_key(version)),
                ).fetchone()
                if blob is not None and blob[0] != "empty":
                    channel_values[channel] = self.serde.loads_typed((blob[0], blob[1]))
            checkpoint = {**checkpoint, "channel_values": channel_values}

            raw_writes = connection.execute(
                "SELECT task_id, channel, value_type, value_data FROM langgraph_checkpoint_writes "
                "WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=? ORDER BY rowid",
                (thread_id, checkpoint_ns, checkpoint_id),
            ).fetchall()

        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint=checkpoint,
            metadata=self.serde.loads_typed((metadata_type, metadata_data)),
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_id,
                    }
                }
                if parent_id
                else None
            ),
            pending_writes=[
                (task_id, channel, self.serde.loads_typed((value_type, value_data)))
                for task_id, channel, value_type, value_data in raw_writes
            ],
        )

    def list(
        self,
        config: Any,
        *,
        filter: dict[str, Any] | None = None,
        before: Any = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if limit is not None and limit <= 0:
            return iter(())

        parameters: list[Any] = []
        clauses: list[str] = []
        if config is not None:
            thread_id, checkpoint_ns = self._thread_config(config)
            clauses.extend(("thread_id=?", "checkpoint_ns=?"))
            parameters.extend((thread_id, checkpoint_ns))
            requested_id = get_checkpoint_id(config)
            if requested_id:
                clauses.append("checkpoint_id=?")
                parameters.append(requested_id)
        if before is not None:
            before_id = get_checkpoint_id(before)
            if before_id:
                clauses.append("checkpoint_id<?")
                parameters.append(before_id)

        sql = "SELECT thread_id, checkpoint_ns, checkpoint_id FROM langgraph_checkpoints"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY checkpoint_id DESC"
        if limit is not None and not filter:
            sql += " LIMIT ?"
            parameters.append(limit)

        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
            configs = [
                {"configurable": {"thread_id": row[0], "checkpoint_ns": row[1], "checkpoint_id": row[2]}}
                for row in rows
            ]

        tuples = [item for item_config in configs if (item := self.get_tuple(item_config)) is not None]
        if filter:
            tuples = [
                item
                for item in tuples
                if all(item.metadata.get(key) == value for key, value in filter.items())
            ]
        if limit is not None:
            tuples = tuples[:limit]
        return iter(tuples)

    def put(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        thread_id, checkpoint_ns = self._thread_config(config)
        checkpoint_id = str(checkpoint["id"])
        parent_id = config["configurable"].get("checkpoint_id")
        checkpoint_payload = dict(checkpoint)
        channel_values = checkpoint_payload.pop("channel_values", {})
        checkpoint_type, checkpoint_data = self.serde.dumps_typed(checkpoint_payload)
        stored_metadata = get_checkpoint_metadata(config, metadata)
        metadata_type, metadata_data = self.serde.dumps_typed(stored_metadata)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO langgraph_checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, checkpoint_type, "
                "checkpoint_data, metadata_type, metadata_data) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(thread_id, checkpoint_ns, checkpoint_id) DO UPDATE SET "
                "parent_checkpoint_id=excluded.parent_checkpoint_id, checkpoint_type=excluded.checkpoint_type, "
                "checkpoint_data=excluded.checkpoint_data, metadata_type=excluded.metadata_type, "
                "metadata_data=excluded.metadata_data",
                (
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                    parent_id,
                    checkpoint_type,
                    checkpoint_data,
                    metadata_type,
                    metadata_data,
                ),
            )
            for channel, version in new_versions.items():
                if channel in channel_values:
                    value_type, value_data = self.serde.dumps_typed(channel_values[channel])
                else:
                    value_type, value_data = "empty", b""
                connection.execute(
                    "INSERT INTO langgraph_checkpoint_blobs "
                    "(thread_id, checkpoint_ns, channel, version_key, value_type, value_data) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(thread_id, checkpoint_ns, channel, version_key) "
                    "DO UPDATE SET value_type=excluded.value_type, value_data=excluded.value_data",
                    (thread_id, checkpoint_ns, channel, self._version_key(version), value_type, value_data),
                )
            connection.commit()

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    def put_writes(self, config: Any, writes: Any, task_id: str, task_path: str = "") -> None:
        thread_id, checkpoint_ns = self._thread_config(config)
        checkpoint_id = str(config["configurable"]["checkpoint_id"])
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for index, (channel, value) in enumerate(writes):
                write_index = WRITES_IDX_MAP.get(channel, index)
                exists = connection.execute(
                    "SELECT 1 FROM langgraph_checkpoint_writes WHERE thread_id=? AND checkpoint_ns=? "
                    "AND checkpoint_id=? AND task_id=? AND write_index=?",
                    (thread_id, checkpoint_ns, checkpoint_id, task_id, write_index),
                ).fetchone()
                if write_index >= 0 and exists:
                    continue
                value_type, value_data = self.serde.dumps_typed(value)
                connection.execute(
                    "INSERT INTO langgraph_checkpoint_writes "
                    "(thread_id, checkpoint_ns, checkpoint_id, task_id, write_index, channel, value_type, value_data, task_path) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(thread_id, checkpoint_ns, checkpoint_id, task_id, write_index) DO UPDATE SET "
                    "channel=excluded.channel, value_type=excluded.value_type, value_data=excluded.value_data, "
                    "task_path=excluded.task_path",
                    (
                        thread_id,
                        checkpoint_ns,
                        checkpoint_id,
                        task_id,
                        write_index,
                        channel,
                        value_type,
                        value_data,
                        task_path,
                    ),
                )
            connection.commit()

    def delete_thread(self, thread_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM langgraph_checkpoint_writes WHERE thread_id=?", (thread_id,))
            connection.execute("DELETE FROM langgraph_checkpoint_blobs WHERE thread_id=?", (thread_id,))
            connection.execute("DELETE FROM langgraph_checkpoints WHERE thread_id=?", (thread_id,))
            connection.commit()

    async def aget_tuple(self, config: Any) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: Any,
        *,
        filter: dict[str, Any] | None = None,
        before: Any = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        items = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for item in items:
            yield item

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config: Any, writes: Any, task_id: str, task_path: str = "") -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)
