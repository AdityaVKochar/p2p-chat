from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from .models import Peer


# Outgoing statuses only move forward, except that a failed message may be
# retried. Incoming messages move from "received" to "read".
STATUS_RANK = {
    "failed": 0,
    "pending": 1,
    "queued": 2,
    "sent": 3,
    "delivered": 4,
    "received": 4,
    "read": 5,
}

TRANSFER_FIELDS = (
    "transfer_id",
    "peer_id",
    "message_id",
    "direction",
    "name",
    "size",
    "mime",
    "sha256",
    "path",
    "file_key",
    "chunk_size",
    "bytes_done",
    "status",
    "error",
    "created_at",
    "updated_at",
)


class ChatStore:
    """Local conversation history, contacts, and file-transfer state."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path
        self._lock = threading.RLock()
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS contacts (
                    node_id TEXT PRIMARY KEY,
                    peer_json TEXT NOT NULL,
                    nickname TEXT NOT NULL DEFAULT '',
                    verified INTEGER NOT NULL DEFAULT 0,
                    last_seen REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    peer_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '',
                    sent_at REAL NOT NULL,
                    stored_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    mode TEXT,
                    transfer_id TEXT
                );
                CREATE INDEX IF NOT EXISTS messages_by_peer ON messages (peer_id, sent_at);
                CREATE TABLE IF NOT EXISTS transfers (
                    transfer_id TEXT PRIMARY KEY,
                    peer_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    name TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mime TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    path TEXT NOT NULL,
                    file_key TEXT NOT NULL,
                    chunk_size INTEGER NOT NULL,
                    bytes_done INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # Contacts -------------------------------------------------------------

    def upsert_contact(self, peer: Peer, *, last_seen: float | None = None) -> None:
        seen = time.time() if last_seen is None else last_seen
        record = json.dumps(peer.to_dict(), sort_keys=True)
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO contacts (node_id, peer_json, last_seen) VALUES (?, ?, ?)
                ON CONFLICT (node_id) DO UPDATE SET
                    peer_json = excluded.peer_json,
                    last_seen = MAX(contacts.last_seen, excluded.last_seen)
                """,
                (peer.node_id, record, seen),
            )

    def get_contact(self, node_id: str) -> Peer | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT peer_json, last_seen FROM contacts WHERE node_id = ?",
                (node_id,),
            ).fetchone()
        return self._peer_from_row(row)

    def contact(self, node_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT * FROM contacts WHERE node_id = ?", (node_id,)).fetchone()
        if row is None:
            return None
        peer = self._peer_from_row(row)
        if peer is None:
            return None
        return {
            "peer": peer,
            "nickname": row["nickname"],
            "verified": bool(row["verified"]),
            "last_seen": row["last_seen"],
        }

    def contacts(self) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute("SELECT node_id FROM contacts").fetchall()
        return [entry for row in rows if (entry := self.contact(row["node_id"])) is not None]

    def _peer_from_row(self, row: sqlite3.Row | None) -> Peer | None:
        if row is None:
            return None
        try:
            peer = Peer.from_dict(json.loads(row["peer_json"]))
        except (KeyError, TypeError, ValueError):
            return None
        peer.last_seen = float(row["last_seen"])
        return peer

    def set_nickname(self, node_id: str, nickname: str) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE contacts SET nickname = ? WHERE node_id = ?",
                (nickname.strip()[:64], node_id),
            )
            return cursor.rowcount > 0

    def set_verified(self, node_id: str, verified: bool) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE contacts SET verified = ? WHERE node_id = ?",
                (int(verified), node_id),
            )
            return cursor.rowcount > 0

    # Messages -------------------------------------------------------------

    def add_message(
        self,
        *,
        message_id: str,
        peer_id: str,
        direction: str,
        kind: str,
        body: str,
        sent_at: float,
        status: str,
        mode: str | None = None,
        transfer_id: str | None = None,
    ) -> bool:
        """Insert a message; returns False when the ID is already stored."""
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO messages
                    (message_id, peer_id, direction, kind, body, sent_at, stored_at, status, mode, transfer_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, peer_id, direction, kind, body, sent_at, time.time(), status, mode, transfer_id),
            )
            return cursor.rowcount > 0

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                f"{self._message_select()} WHERE m.message_id = ?",
                (message_id,),
            ).fetchone()
        return self._message_from_row(row) if row is not None else None

    def set_status(
        self,
        message_ids: Iterable[str],
        status: str,
        *,
        peer_id: str | None = None,
        direction: str | None = None,
        mode: str | None = None,
        force: bool = False,
    ) -> list[str]:
        """Advance message statuses and return the IDs that changed.

        Unless *force* is set, a status never moves backwards, so a late
        "delivered" receipt cannot overwrite "read". When *peer_id* is given,
        only messages exchanged with that peer are touched, and *direction*
        likewise restricts the update to incoming or outgoing messages.
        """
        rank = STATUS_RANK[status]
        changed: list[str] = []
        with self._lock, self._connection() as connection:
            for message_id in dict.fromkeys(str(value)[:128] for value in message_ids):
                row = connection.execute(
                    "SELECT peer_id, direction, status FROM messages WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                if row is None or (peer_id is not None and row["peer_id"] != peer_id):
                    continue
                if direction is not None and row["direction"] != direction:
                    continue
                if not force and STATUS_RANK.get(row["status"], 0) >= rank:
                    continue
                connection.execute(
                    "UPDATE messages SET status = ?, mode = COALESCE(?, mode) WHERE message_id = ?",
                    (status, mode, message_id),
                )
                changed.append(message_id)
        return changed

    @staticmethod
    def _message_select() -> str:
        return "SELECT m.*, t.transfer_id AS t_id, " + ", ".join(
            f"t.{field} AS t_{field}" for field in TRANSFER_FIELDS if field != "transfer_id"
        ) + " FROM messages m LEFT JOIN transfers t ON t.transfer_id = m.transfer_id"

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> dict[str, Any]:
        message: dict[str, Any] = {
            "message_id": row["message_id"],
            "peer_id": row["peer_id"],
            "direction": row["direction"],
            "kind": row["kind"],
            "text": row["body"],
            "sent_at": row["sent_at"],
            "status": row["status"],
            "mode": row["mode"],
            "transfer": None,
        }
        if row["t_id"] is not None:
            message["transfer"] = public_transfer(
                {"transfer_id": row["t_id"], **{field: row[f"t_{field}"] for field in TRANSFER_FIELDS[1:]}}
            )
        return message

    def history(self, peer_id: str, *, before: float | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Return up to *limit* messages older than *before*, oldest first."""
        limit = min(max(int(limit), 1), 500)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                {self._message_select()}
                WHERE m.peer_id = ? AND m.sent_at < ?
                ORDER BY m.sent_at DESC, m.stored_at DESC
                LIMIT ?
                """,
                (peer_id, float("inf") if before is None else before, limit),
            ).fetchall()
        return [self._message_from_row(row) for row in reversed(rows)]

    def mark_read(self, peer_id: str) -> list[str]:
        """Mark every received message from *peer_id* as read; returns their IDs."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT message_id FROM messages WHERE peer_id = ? AND direction = 'incoming' AND status = 'received'",
                (peer_id,),
            ).fetchall()
            ids = [row["message_id"] for row in rows]
            connection.execute(
                "UPDATE messages SET status = 'read' WHERE peer_id = ? AND direction = 'incoming' AND status = 'received'",
                (peer_id,),
            )
        return ids

    def unread_total(self) -> int:
        with self._lock, self._connection() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE direction = 'incoming' AND status = 'received'"
                ).fetchone()[0]
            )

    def list_conversations(self) -> list[dict[str, Any]]:
        """Return contacts that have messages, most recently active first."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    m.peer_id,
                    MAX(m.sent_at) AS last_activity,
                    SUM(CASE WHEN m.direction = 'incoming' AND m.status = 'received' THEN 1 ELSE 0 END) AS unread
                FROM messages m
                GROUP BY m.peer_id
                ORDER BY last_activity DESC
                """
            ).fetchall()
            conversations: list[dict[str, Any]] = []
            for row in rows:
                last = connection.execute(
                    f"""
                    {self._message_select()}
                    WHERE m.peer_id = ?
                    ORDER BY m.sent_at DESC, m.stored_at DESC
                    LIMIT 1
                    """,
                    (row["peer_id"],),
                ).fetchone()
                conversations.append(
                    {
                        "peer_id": row["peer_id"],
                        "last_activity": row["last_activity"],
                        "unread": int(row["unread"] or 0),
                        "last_message": self._message_from_row(last) if last is not None else None,
                    }
                )
        return conversations

    def delete_conversation(self, peer_id: str) -> list[dict[str, Any]]:
        """Delete messages and transfers for *peer_id*; returns the removed transfers."""
        with self._lock, self._connection() as connection:
            transfers = [
                dict(row)
                for row in connection.execute("SELECT * FROM transfers WHERE peer_id = ?", (peer_id,)).fetchall()
            ]
            connection.execute("DELETE FROM messages WHERE peer_id = ?", (peer_id,))
            connection.execute("DELETE FROM transfers WHERE peer_id = ?", (peer_id,))
        return transfers

    # Transfers ------------------------------------------------------------

    def add_transfer(self, transfer: dict[str, Any]) -> bool:
        now = time.time()
        values = {"bytes_done": 0, "error": "", "created_at": now, "updated_at": now, **transfer}
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                f"""
                INSERT OR IGNORE INTO transfers ({", ".join(TRANSFER_FIELDS)})
                VALUES ({", ".join("?" for _ in TRANSFER_FIELDS)})
                """,
                [values[field] for field in TRANSFER_FIELDS],
            )
            return cursor.rowcount > 0

    def get_transfer(self, transfer_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT * FROM transfers WHERE transfer_id = ?", (transfer_id,)).fetchone()
        return dict(row) if row is not None else None

    def update_transfer(self, transfer_id: str, **fields: Any) -> None:
        unknown = set(fields) - set(TRANSFER_FIELDS)
        if unknown:
            raise ValueError(f"unknown transfer fields: {sorted(unknown)}")
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._lock, self._connection() as connection:
            connection.execute(
                f"UPDATE transfers SET {assignments} WHERE transfer_id = ?",
                [*fields.values(), transfer_id],
            )

    def pending_outgoing_transfers(self, peer_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM transfers WHERE direction = 'outgoing' AND status IN ('waiting', 'sending')"
        parameters: list[Any] = []
        if peer_id is not None:
            query += " AND peer_id = ?"
            parameters.append(peer_id)
        with self._lock, self._connection() as connection:
            rows = connection.execute(query + " ORDER BY created_at ASC", parameters).fetchall()
        return [dict(row) for row in rows]

    def interrupt_active_transfers(self) -> None:
        """Reset transfers that were mid-flight when the node last stopped."""
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE transfers SET status = 'waiting' WHERE direction = 'outgoing' AND status = 'sending'"
            )


def public_transfer(transfer: dict[str, Any]) -> dict[str, Any]:
    """Strip the file key before a transfer record leaves the networking engine."""
    return {key: value for key, value in transfer.items() if key != "file_key"}
