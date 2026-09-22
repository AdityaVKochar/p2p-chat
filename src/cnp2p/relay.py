from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
from contextlib import contextmanager
from collections.abc import Iterator

from .crypto import canonical_json


MAX_ENVELOPE_BYTES = 32_768
MAX_QUEUED_PER_RECIPIENT = 1_000
DEFAULT_MESSAGE_TTL = 7 * 24 * 60 * 60


class RelayStoreError(ValueError):
    pass


class RelayStore:
    """Persistent ciphertext-only store used by relay-enabled nodes."""

    def __init__(self, database_path: Path, *, ttl: float = DEFAULT_MESSAGE_TTL) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path
        self.ttl = ttl
        self._lock = threading.RLock()
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS relay_messages (
                    recipient_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    queued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (recipient_id, message_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS relay_expiry ON relay_messages (expires_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path, timeout=5)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def cleanup(self) -> int:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM relay_messages WHERE expires_at <= ?",
                (time.time(),),
            )
            return cursor.rowcount

    def store(self, envelope: dict[str, Any]) -> bool:
        encoded = canonical_json(envelope)
        if len(encoded) > MAX_ENVELOPE_BYTES:
            raise RelayStoreError("encrypted envelope exceeds relay size limit")
        recipient_id = str(envelope["recipient_id"])
        message_id = str(envelope["message_id"])
        now = time.time()
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM relay_messages WHERE expires_at <= ?", (now,))
            existing = connection.execute(
                "SELECT 1 FROM relay_messages WHERE recipient_id = ? AND message_id = ?",
                (recipient_id, message_id),
            ).fetchone()
            if existing:
                return False
            count = connection.execute(
                "SELECT COUNT(*) FROM relay_messages WHERE recipient_id = ?",
                (recipient_id,),
            ).fetchone()[0]
            if count >= MAX_QUEUED_PER_RECIPIENT:
                raise RelayStoreError("recipient offline queue is full")
            connection.execute(
                """
                INSERT INTO relay_messages
                    (recipient_id, message_id, envelope_json, queued_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (recipient_id, message_id, encoded.decode("utf-8"), now, now + self.ttl),
            )
            return True

    def fetch(self, recipient_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM relay_messages WHERE expires_at <= ?", (now,))
            rows = connection.execute(
                """
                SELECT envelope_json
                FROM relay_messages
                WHERE recipient_id = ?
                ORDER BY queued_at ASC
                LIMIT ?
                """,
                (recipient_id, min(max(limit, 1), 100)),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete(self, recipient_id: str, message_ids: list[str]) -> int:
        if not message_ids:
            return 0
        cleaned_ids = [str(message_id)[:128] for message_id in message_ids[:100]]
        placeholders = ",".join("?" for _ in cleaned_ids)
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                f"DELETE FROM relay_messages WHERE recipient_id = ? AND message_id IN ({placeholders})",
                [recipient_id, *cleaned_ids],
            )
            return cursor.rowcount

    def count(self, recipient_id: str | None = None) -> int:
        with self._lock, self._connection() as connection:
            if recipient_id is None:
                return int(connection.execute("SELECT COUNT(*) FROM relay_messages").fetchone()[0])
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM relay_messages WHERE recipient_id = ?",
                    (recipient_id,),
                ).fetchone()[0]
            )
