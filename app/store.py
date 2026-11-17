"""SQLite persistence for conversations.

Deliberately boring: one file, two tables, WAL mode, a fresh connection per
operation. No ORM, no migrations framework -- the schema is created on first run.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

UNTITLED = "新对话"
MAX_TITLE = 40


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS conversations (
                    id          TEXT PRIMARY KEY,
                    title       TEXT NOT NULL,
                    persona     TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id  TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role              TEXT NOT NULL,
                    content           TEXT NOT NULL,
                    provider          TEXT,
                    created_at        TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON messages(conversation_id, id);
                """
            )

    # -- conversations ----------------------------------------------------
    def create_conversation(self, persona: str, title: str = UNTITLED) -> Dict[str, Any]:
        conversation_id = uuid.uuid4().hex[:12]
        timestamp = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO conversations (id, title, persona, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, title, persona, timestamp, timestamp),
            )
        return {
            "id": conversation_id,
            "title": title,
            "persona": persona,
            "created_at": timestamp,
            "updated_at": timestamp,
            "message_count": 0,
        }

    def list_conversations(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.title, c.persona, c.created_at, c.updated_at,
                       COUNT(m.id) AS message_count
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                GROUP BY c.id
                ORDER BY c.updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, title, persona, created_at, updated_at FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if row is None:
                return None
            messages = connection.execute(
                "SELECT id, role, content, provider, created_at FROM messages WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        payload = dict(row)
        payload["messages"] = [dict(message) for message in messages]
        payload["message_count"] = len(payload["messages"])
        return payload

    def rename_conversation(self, conversation_id: str, title: str) -> bool:
        clean = " ".join(title.split())[:MAX_TITLE] or UNTITLED
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (clean, _now(), conversation_id),
            )
        return cursor.rowcount > 0

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._connect() as connection:
            connection.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            cursor = connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return cursor.rowcount > 0

    # -- messages ---------------------------------------------------------
    def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        provider: Optional[str] = None,
    ) -> None:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO messages (conversation_id, role, content, provider, created_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, role, content, provider, timestamp),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (timestamp, conversation_id),
            )

    def messages_for_model(self, conversation_id: str) -> List[Dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        return [
            {"role": row["role"], "content": row["content"]}
            for row in rows
            if row["role"] in {"user", "assistant"}
        ]

    def auto_title(self, conversation_id: str, first_message: str) -> str:
        """Give a fresh conversation a readable title from its first turn."""
        title = " ".join(first_message.split())[:MAX_TITLE] or UNTITLED
        with self._connect() as connection:
            row = connection.execute(
                "SELECT title FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if row is not None and row["title"] in {UNTITLED, ""}:
                connection.execute(
                    "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                    (title, _now(), conversation_id),
                )
                return title
        return row["title"] if row is not None else title
