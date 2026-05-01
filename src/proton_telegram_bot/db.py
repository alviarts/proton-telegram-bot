"""Async SQLite persistence layer."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from .models import AliasRecord, AliasStatus, BridgeCredentials, UserRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    imap_host TEXT,
    imap_port INTEGER,
    imap_username TEXT,
    imap_password_encrypted TEXT,
    imap_use_ssl INTEGER NOT NULL DEFAULT 0,
    active_alias_id INTEGER REFERENCES aliases(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    last_message_id TEXT,
    UNIQUE(chat_id, email),
    FOREIGN KEY(chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_aliases_chat_status ON aliases(chat_id, status);
"""


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    """Lightweight async wrapper around a single SQLite connection."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Apply idempotent schema migrations for older databases."""
        async with self.conn.execute("PRAGMA table_info(users)") as cursor:
            cols = {row["name"] for row in await cursor.fetchall()}
        if "active_alias_id" not in cols:
            await self.conn.execute(
                "ALTER TABLE users ADD COLUMN active_alias_id INTEGER"
            )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected. Call connect() first.")
        return self._conn

    # ------------------------------------------------------------------ users

    async def upsert_user(self, chat_id: int) -> None:
        await self.conn.execute(
            "INSERT INTO users (chat_id, created_at) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO NOTHING",
            (chat_id, _utcnow()),
        )
        await self.conn.commit()

    async def set_credentials(
        self,
        chat_id: int,
        host: str,
        port: int,
        username: str,
        encrypted_password: str,
        use_ssl: bool,
    ) -> None:
        await self.upsert_user(chat_id)
        await self.conn.execute(
            "UPDATE users SET imap_host = ?, imap_port = ?, imap_username = ?, "
            "imap_password_encrypted = ?, imap_use_ssl = ? WHERE chat_id = ?",
            (host, port, username, encrypted_password, 1 if use_ssl else 0, chat_id),
        )
        await self.conn.commit()

    async def clear_credentials(self, chat_id: int) -> None:
        await self.conn.execute(
            "UPDATE users SET imap_host = NULL, imap_port = NULL, imap_username = NULL, "
            "imap_password_encrypted = NULL, imap_use_ssl = 0 WHERE chat_id = ?",
            (chat_id,),
        )
        await self.conn.commit()

    async def get_user(self, chat_id: int) -> UserRecord | None:
        async with self.conn.execute(
            "SELECT chat_id, imap_host, imap_port, imap_username, imap_password_encrypted, "
            "imap_use_ssl FROM users WHERE chat_id = ?",
            (chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return UserRecord(
            chat_id=row["chat_id"],
            has_credentials=row["imap_password_encrypted"] is not None,
            imap_host=row["imap_host"],
            imap_port=row["imap_port"],
            imap_username=row["imap_username"],
            imap_use_ssl=bool(row["imap_use_ssl"]),
        )

    async def get_encrypted_password(self, chat_id: int) -> str | None:
        async with self.conn.execute(
            "SELECT imap_password_encrypted FROM users WHERE chat_id = ?",
            (chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return row["imap_password_encrypted"]

    async def set_active_alias(self, chat_id: int, alias_id: int | None) -> None:
        await self.upsert_user(chat_id)
        await self.conn.execute(
            "UPDATE users SET active_alias_id = ? WHERE chat_id = ?",
            (alias_id, chat_id),
        )
        await self.conn.commit()

    async def get_active_alias(self, chat_id: int) -> AliasRecord | None:
        async with self.conn.execute(
            "SELECT active_alias_id FROM users WHERE chat_id = ?",
            (chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["active_alias_id"] is None:
            return None
        return await self.find_alias_by_id(chat_id, row["active_alias_id"])

    async def list_users_with_credentials(self) -> list[int]:
        async with self.conn.execute(
            "SELECT chat_id FROM users WHERE imap_password_encrypted IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
        return [row["chat_id"] for row in rows]

    # ---------------------------------------------------------------- aliases

    async def add_aliases(self, chat_id: int, emails: Sequence[str]) -> int:
        """Insert aliases ignoring duplicates. Returns the number of new rows."""
        await self.upsert_user(chat_id)
        inserted = 0
        for email in emails:
            normalized = email.strip().lower()
            if not normalized:
                continue
            cursor = await self.conn.execute(
                "INSERT OR IGNORE INTO aliases (chat_id, email, status, created_at) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, normalized, AliasStatus.AVAILABLE.value, _utcnow()),
            )
            inserted += cursor.rowcount or 0
        await self.conn.commit()
        return inserted

    async def remove_alias(self, chat_id: int, email: str) -> bool:
        cursor = await self.conn.execute(
            "DELETE FROM aliases WHERE chat_id = ? AND email = ?",
            (chat_id, email.strip().lower()),
        )
        await self.conn.commit()
        return (cursor.rowcount or 0) > 0

    async def list_aliases(
        self, chat_id: int, status: AliasStatus | None = None
    ) -> list[AliasRecord]:
        query = (
            "SELECT id, chat_id, email, status, consumed_at, last_message_id FROM aliases "
            "WHERE chat_id = ?"
        )
        params: list[object] = [chat_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        query += " ORDER BY email"
        async with self.conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [
            AliasRecord(
                id=row["id"],
                chat_id=row["chat_id"],
                email=row["email"],
                status=AliasStatus(row["status"]),
                consumed_at=row["consumed_at"],
                last_message_id=row["last_message_id"],
            )
            for row in rows
        ]

    async def find_alias(self, chat_id: int, email: str) -> AliasRecord | None:
        return await self._find_alias(
            "WHERE chat_id = ? AND email = ?",
            (chat_id, email.strip().lower()),
        )

    async def find_alias_by_id(self, chat_id: int, alias_id: int) -> AliasRecord | None:
        return await self._find_alias(
            "WHERE chat_id = ? AND id = ?",
            (chat_id, alias_id),
        )

    async def _find_alias(
        self, where_clause: str, params: tuple[object, ...]
    ) -> AliasRecord | None:
        query = (
            "SELECT id, chat_id, email, status, consumed_at, last_message_id FROM aliases "
            f"{where_clause}"
        )
        async with self.conn.execute(query, params) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return AliasRecord(
            id=row["id"],
            chat_id=row["chat_id"],
            email=row["email"],
            status=AliasStatus(row["status"]),
            consumed_at=row["consumed_at"],
            last_message_id=row["last_message_id"],
        )

    async def mark_consumed(self, alias_id: int, message_id: str | None) -> None:
        await self.conn.execute(
            "UPDATE aliases SET status = ?, consumed_at = ?, last_message_id = ? WHERE id = ?",
            (AliasStatus.CONSUMED.value, _utcnow(), message_id, alias_id),
        )
        await self.conn.commit()

    async def reset_alias(self, chat_id: int, email: str) -> bool:
        cursor = await self.conn.execute(
            "UPDATE aliases SET status = ?, consumed_at = NULL, last_message_id = NULL "
            "WHERE chat_id = ? AND email = ?",
            (AliasStatus.AVAILABLE.value, chat_id, email.strip().lower()),
        )
        await self.conn.commit()
        return (cursor.rowcount or 0) > 0


def credentials_from_user(user: UserRecord, plaintext_password: str) -> BridgeCredentials:
    """Reconstruct a BridgeCredentials object after decrypting the stored password."""
    if not (user.imap_host and user.imap_port and user.imap_username):
        raise ValueError("user does not have IMAP credentials configured")
    return BridgeCredentials(
        host=user.imap_host,
        port=user.imap_port,
        username=user.imap_username,
        password=plaintext_password,
        use_ssl=user.imap_use_ssl,
    )
