"""Async SQLite persistence layer."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from .alias_gen import GenState
from .models import (
    AliasRecord,
    AliasSenderRecord,
    AliasStatus,
    BridgeCredentials,
    PrimaryAccount,
    UserRecord,
)

# Built-in domain → friendly-label fallback. Chat-level overrides
# (``service_labels`` table) take priority; this dict is the last resort.
DEFAULT_SERVICE_LABELS: dict[str, str] = {
    "cognition.ai": "Devin",
    "github.com": "GitHub",
    "google.com": "Google",
    "accounts.google.com": "Google",
    "discord.com": "Discord",
    "openai.com": "OpenAI",
    "chat.openai.com": "OpenAI",
    "linkedin.com": "LinkedIn",
    "twitter.com": "X/Twitter",
    "x.com": "X/Twitter",
    "facebook.com": "Facebook",
    "meta.com": "Meta",
    "instagram.com": "Instagram",
    "apple.com": "Apple",
    "microsoft.com": "Microsoft",
    "live.com": "Microsoft",
    "outlook.com": "Microsoft",
    "amazon.com": "Amazon",
    "aws.amazon.com": "AWS",
    "stripe.com": "Stripe",
    "netlify.com": "Netlify",
    "vercel.com": "Vercel",
    "cloudflare.com": "Cloudflare",
    "digitalocean.com": "DigitalOcean",
    "heroku.com": "Heroku",
    "gitlab.com": "GitLab",
    "bitbucket.org": "Bitbucket",
    "notion.so": "Notion",
    "slack.com": "Slack",
    "telegram.org": "Telegram",
    "reddit.com": "Reddit",
    "stackoverflow.com": "StackOverflow",
    "npm.io": "npm",
    "npmjs.com": "npm",
    "pypi.org": "PyPI",
    "docker.com": "Docker",
    "fly.io": "Fly.io",
    "railway.app": "Railway",
    "supabase.io": "Supabase",
    "supabase.com": "Supabase",
    "firebase.google.com": "Firebase",
    "sentry.io": "Sentry",
    "datadog.com": "Datadog",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    imap_host TEXT,
    imap_port INTEGER,
    imap_username TEXT,
    imap_password_encrypted TEXT,
    imap_use_ssl INTEGER NOT NULL DEFAULT 0,
    active_alias_id INTEGER REFERENCES aliases(id) ON DELETE SET NULL,
    active_primary_id INTEGER REFERENCES primary_accounts(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    last_message_id TEXT,
    primary_id INTEGER,
    UNIQUE(chat_id, email),
    FOREIGN KEY(chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS primary_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    imap_host TEXT NOT NULL,
    imap_port INTEGER NOT NULL,
    imap_username TEXT NOT NULL,
    imap_password_encrypted TEXT NOT NULL,
    imap_use_ssl INTEGER NOT NULL DEFAULT 0,
    proton_password_encrypted TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(chat_id, email),
    FOREIGN KEY(chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
);

-- Tracks the next ``(suffix, number)`` cursor that ``/genaddr`` should emit
-- for each ``(chat_id, primary_id, base)``. See ``alias_gen.py`` for the
-- pattern logic.
CREATE TABLE IF NOT EXISTS alias_generator_state (
    chat_id INTEGER NOT NULL,
    primary_id INTEGER NOT NULL,
    base TEXT NOT NULL,
    next_suffix TEXT NOT NULL DEFAULT '',
    next_number INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, primary_id, base),
    FOREIGN KEY(chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
);

-- Records unique (alias, sender_domain) pairs from incoming emails so each
-- alias can be labelled with the services that have used it ("Devin",
-- "GitHub", …). One row per alias+domain combination — we de-duplicate
-- at insert time using ON CONFLICT.
CREATE TABLE IF NOT EXISTS alias_senders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    sender_email TEXT,
    sender_domain TEXT NOT NULL,
    service_label TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    UNIQUE(alias_id, sender_domain),
    FOREIGN KEY(alias_id) REFERENCES aliases(id) ON DELETE CASCADE,
    FOREIGN KEY(chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
);

-- Per-chat domain → friendly label mapping. The user can create/edit
-- these via /services; they take priority over the built-in defaults
-- in ``DEFAULT_SERVICE_LABELS``. A row with ``label = ''`` is an
-- explicit "suppress" sentinel written by the inline 🗑 Hapus label
-- button so the resolver returns ``None`` even when a built-in default
-- would otherwise apply.
CREATE TABLE IF NOT EXISTS service_labels (
    chat_id INTEGER NOT NULL,
    domain TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, domain),
    FOREIGN KEY(chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
);

-- Telegram message ids of every email forwarded to a chat, keyed by the
-- alias the email was destined for. We use this to bulk-delete the
-- "old" alias' messages when the user switches their active alias via
-- /list or /unlock, so chats don't fill up with stale forwards.
CREATE TABLE IF NOT EXISTS forwarded_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    alias_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    FOREIGN KEY(chat_id) REFERENCES users(chat_id) ON DELETE CASCADE,
    FOREIGN KEY(alias_id) REFERENCES aliases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_aliases_chat_status ON aliases(chat_id, status);
CREATE INDEX IF NOT EXISTS idx_primary_chat ON primary_accounts(chat_id);
CREATE INDEX IF NOT EXISTS idx_alias_senders_alias ON alias_senders(alias_id);
CREATE INDEX IF NOT EXISTS idx_alias_senders_chat ON alias_senders(chat_id);
CREATE INDEX IF NOT EXISTS idx_forwarded_emails_chat_alias
    ON forwarded_emails(chat_id, alias_id);
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
            user_cols = {row["name"] for row in await cursor.fetchall()}
        if "active_alias_id" not in user_cols:
            await self.conn.execute(
                "ALTER TABLE users ADD COLUMN active_alias_id INTEGER"
            )
        if "active_primary_id" not in user_cols:
            # Tracks which primary account is the "current" one for this
            # chat. Set by ``/setprotonpw`` when the user picks an account
            # and consumed by ``/genaddr`` so the next batch runs against
            # the same primary the user just configured.
            await self.conn.execute(
                "ALTER TABLE users ADD COLUMN active_primary_id INTEGER"
            )
        async with self.conn.execute("PRAGMA table_info(aliases)") as cursor:
            alias_cols = {row["name"] for row in await cursor.fetchall()}
        if "primary_id" not in alias_cols:
            await self.conn.execute(
                "ALTER TABLE aliases ADD COLUMN primary_id INTEGER"
            )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_aliases_primary ON aliases(primary_id)"
        )
        async with self.conn.execute("PRAGMA table_info(primary_accounts)") as cursor:
            primary_cols = {row["name"] for row in await cursor.fetchall()}
        if "proton_password_encrypted" not in primary_cols:
            # Older DBs predate /setprotonpw — add the column nullable so the
            # migration is non-destructive. Users top-up the value via the new
            # command before /genaddr can run.
            await self.conn.execute(
                "ALTER TABLE primary_accounts ADD COLUMN proton_password_encrypted TEXT"
            )
        # Last /cekimap result, surfaced in the /list keyboard as
        # ``ok/total`` so the user can spot a primary whose aliases
        # have started failing without re-running the check.
        if "last_healthcheck_ok" not in primary_cols:
            await self.conn.execute(
                "ALTER TABLE primary_accounts ADD COLUMN last_healthcheck_ok INTEGER"
            )
        if "last_healthcheck_total" not in primary_cols:
            await self.conn.execute(
                "ALTER TABLE primary_accounts ADD COLUMN last_healthcheck_total INTEGER"
            )
        if "last_healthcheck_at" not in primary_cols:
            await self.conn.execute(
                "ALTER TABLE primary_accounts ADD COLUMN last_healthcheck_at TEXT"
            )
        await self._backfill_primary_accounts_from_legacy_users()

    async def _backfill_primary_accounts_from_legacy_users(self) -> None:
        """Promote legacy ``users.imap_*`` credentials into ``primary_accounts``.

        The original schema stored a single Bridge login per chat directly on
        the ``users`` row. The new model allows multiple primaries per chat,
        each with its own listener. For backwards compatibility we promote
        any chat whose ``users`` row has credentials but no matching
        ``primary_accounts`` row, then link its existing aliases to the new
        primary. Idempotent — running this on an already-migrated DB is a
        no-op.
        """
        async with self.conn.execute(
            "SELECT chat_id, imap_host, imap_port, imap_username, "
            "imap_password_encrypted, imap_use_ssl, created_at FROM users "
            "WHERE imap_password_encrypted IS NOT NULL"
        ) as cursor:
            legacy_rows = await cursor.fetchall()
        for row in legacy_rows:
            chat_id = row["chat_id"]
            email = (row["imap_username"] or "").strip().lower()
            if not email:
                continue
            async with self.conn.execute(
                "SELECT id FROM primary_accounts WHERE chat_id = ? AND email = ?",
                (chat_id, email),
            ) as existing:
                already = await existing.fetchone()
            if already is not None:
                primary_id = already["id"]
            else:
                cursor = await self.conn.execute(
                    "INSERT INTO primary_accounts "
                    "(chat_id, email, imap_host, imap_port, imap_username, "
                    "imap_password_encrypted, imap_use_ssl, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chat_id,
                        email,
                        row["imap_host"],
                        row["imap_port"],
                        row["imap_username"],
                        row["imap_password_encrypted"],
                        row["imap_use_ssl"] or 0,
                        row["created_at"] or _utcnow(),
                    ),
                )
                primary_id = cursor.lastrowid
            # Link any orphan aliases for this chat to the just-created primary.
            await self.conn.execute(
                "UPDATE aliases SET primary_id = ? "
                "WHERE chat_id = ? AND primary_id IS NULL",
                (primary_id, chat_id),
            )
        await self.conn.commit()

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

    async def set_active_primary(
        self, chat_id: int, primary_id: int | None
    ) -> None:
        """Mark ``primary_id`` as the chat's current account.

        Called when the user picks an account in ``/setprotonpw`` so that
        subsequent commands (``/genaddr``, status banners, …) default to
        the account they just configured instead of an arbitrary one.
        """
        await self.upsert_user(chat_id)
        await self.conn.execute(
            "UPDATE users SET active_primary_id = ? WHERE chat_id = ?",
            (primary_id, chat_id),
        )
        await self.conn.commit()

    async def get_active_primary_id(self, chat_id: int) -> int | None:
        async with self.conn.execute(
            "SELECT active_primary_id FROM users WHERE chat_id = ?",
            (chat_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        value = row["active_primary_id"]
        return int(value) if value is not None else None

    async def list_users_with_credentials(self) -> list[int]:
        async with self.conn.execute(
            "SELECT chat_id FROM users WHERE imap_password_encrypted IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
        return [row["chat_id"] for row in rows]

    # ----------------------------------------------------- primary_accounts

    async def add_primary_account(
        self,
        chat_id: int,
        email: str,
        host: str,
        port: int,
        username: str,
        encrypted_password: str,
        use_ssl: bool,
    ) -> int:
        """Insert (or update if it already exists) a primary account.

        Returns the ``primary_accounts.id``.
        """
        await self.upsert_user(chat_id)
        normalized = email.strip().lower()
        await self.conn.execute(
            "INSERT INTO primary_accounts "
            "(chat_id, email, imap_host, imap_port, imap_username, "
            "imap_password_encrypted, imap_use_ssl, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, email) DO UPDATE SET "
            "imap_host = excluded.imap_host, imap_port = excluded.imap_port, "
            "imap_username = excluded.imap_username, "
            "imap_password_encrypted = excluded.imap_password_encrypted, "
            "imap_use_ssl = excluded.imap_use_ssl",
            (
                chat_id,
                normalized,
                host,
                port,
                username,
                encrypted_password,
                1 if use_ssl else 0,
                _utcnow(),
            ),
        )
        await self.conn.commit()
        # ``cursor.lastrowid`` is unreliable for UPSERT — SQLite may keep a
        # stale value from a prior INSERT on the same connection. Always look
        # the row up by its natural key instead.
        async with self.conn.execute(
            "SELECT id FROM primary_accounts WHERE chat_id = ? AND email = ?",
            (chat_id, normalized),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        return row["id"]

    async def list_primary_accounts(self, chat_id: int) -> list[PrimaryAccount]:
        async with self.conn.execute(
            "SELECT id, chat_id, email, imap_host, imap_port, imap_username, "
            "imap_use_ssl, created_at FROM primary_accounts "
            "WHERE chat_id = ? ORDER BY email",
            (chat_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_primary(row) for row in rows]

    async def get_primary_account(
        self, chat_id: int, primary_id: int
    ) -> PrimaryAccount | None:
        async with self.conn.execute(
            "SELECT id, chat_id, email, imap_host, imap_port, imap_username, "
            "imap_use_ssl, created_at FROM primary_accounts "
            "WHERE chat_id = ? AND id = ?",
            (chat_id, primary_id),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_primary(row) if row is not None else None

    async def get_primary_account_by_id(
        self, primary_id: int
    ) -> PrimaryAccount | None:
        async with self.conn.execute(
            "SELECT id, chat_id, email, imap_host, imap_port, imap_username, "
            "imap_use_ssl, created_at FROM primary_accounts WHERE id = ?",
            (primary_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_primary(row) if row is not None else None

    async def get_primary_encrypted_password(self, primary_id: int) -> str | None:
        async with self.conn.execute(
            "SELECT imap_password_encrypted FROM primary_accounts WHERE id = ?",
            (primary_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return row["imap_password_encrypted"] if row is not None else None

    async def delete_primary_account(self, chat_id: int, primary_id: int) -> bool:
        """Hard-delete a primary account and every dependent row.

        Performs a clean-slate purge so a future /connect for the same email
        starts fresh: aliases, generator state, active-alias / active-primary
        pointers, and (when this is the last primary) the legacy per-user
        IMAP credentials are all wiped. ``ON DELETE`` cascades exist on new
        schemas but migrated DBs may have been created before the FKs were
        added, so the deletes are explicit for safety.
        """
        # Aliases for this primary.
        await self.conn.execute(
            "DELETE FROM aliases WHERE chat_id = ? AND primary_id = ?",
            (chat_id, primary_id),
        )
        # /genaddr cursor state for this (chat, primary).
        await self.conn.execute(
            "DELETE FROM alias_generator_state "
            "WHERE chat_id = ? AND primary_id = ?",
            (chat_id, primary_id),
        )
        # Clear the chat's active-alias pointer if it referenced an alias of
        # the primary we just deleted. Old DBs added active_alias_id without
        # the ``ON DELETE SET NULL`` FK so do it explicitly.
        await self.conn.execute(
            "UPDATE users SET active_alias_id = NULL "
            "WHERE chat_id = ? AND active_alias_id NOT IN ("
            "    SELECT id FROM aliases WHERE chat_id = ?"
            ")",
            (chat_id, chat_id),
        )
        # Clear ``active_primary_id`` if it points at the row we're about to
        # delete. Same FK caveat as above.
        await self.conn.execute(
            "UPDATE users SET active_primary_id = NULL "
            "WHERE chat_id = ? AND active_primary_id = ?",
            (chat_id, primary_id),
        )
        cursor = await self.conn.execute(
            "DELETE FROM primary_accounts WHERE chat_id = ? AND id = ?",
            (chat_id, primary_id),
        )
        # If the user has no primaries left, also purge the legacy per-user
        # IMAP fields on the ``users`` row (pre-multi-primary schema). This
        # ensures a future /connect cannot accidentally reuse stale creds.
        async with self.conn.execute(
            "SELECT COUNT(*) AS n FROM primary_accounts WHERE chat_id = ?",
            (chat_id,),
        ) as count_cursor:
            count_row = await count_cursor.fetchone()
        if count_row is not None and count_row["n"] == 0:
            await self.conn.execute(
                "UPDATE users SET imap_host = NULL, imap_port = NULL, "
                "imap_username = NULL, imap_password_encrypted = NULL, "
                "imap_use_ssl = 0 WHERE chat_id = ?",
                (chat_id,),
            )
        await self.conn.commit()
        return (cursor.rowcount or 0) > 0

    async def list_all_primary_ids(self) -> list[int]:
        """Return every primary id across all chats — used by ListenerManager
        to spin up listeners on startup."""
        async with self.conn.execute(
            "SELECT id FROM primary_accounts ORDER BY id"
        ) as cursor:
            rows = await cursor.fetchall()
        return [row["id"] for row in rows]

    # ---------------------------------------------------------------- proton master password

    async def set_proton_password(
        self,
        chat_id: int,
        primary_id: int,
        encrypted_password: str,
    ) -> bool:
        """Persist the Proton master password for a primary account.

        This password is what /genaddr uses to drive the Proton web UI via
        Playwright. It is stored Fernet-encrypted (callers must encrypt
        before passing). Returns ``True`` if the row was found and updated.
        """
        cursor = await self.conn.execute(
            "UPDATE primary_accounts SET proton_password_encrypted = ? "
            "WHERE chat_id = ? AND id = ?",
            (encrypted_password, chat_id, primary_id),
        )
        await self.conn.commit()
        return (cursor.rowcount or 0) > 0

    async def clear_proton_password(self, chat_id: int, primary_id: int) -> bool:
        cursor = await self.conn.execute(
            "UPDATE primary_accounts SET proton_password_encrypted = NULL "
            "WHERE chat_id = ? AND id = ?",
            (chat_id, primary_id),
        )
        await self.conn.commit()
        return (cursor.rowcount or 0) > 0

    async def get_proton_password_encrypted(self, primary_id: int) -> str | None:
        async with self.conn.execute(
            "SELECT proton_password_encrypted FROM primary_accounts WHERE id = ?",
            (primary_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return row["proton_password_encrypted"]

    # ---------------------------------------------------------------- /cekimap stats

    async def set_last_healthcheck(
        self, primary_id: int, ok: int, total: int
    ) -> None:
        """Persist the most recent /cekimap result for a primary so the
        /list keyboard can render ``ok/total`` next to the email.
        """
        await self.conn.execute(
            "UPDATE primary_accounts "
            "SET last_healthcheck_ok = ?, "
            "    last_healthcheck_total = ?, "
            "    last_healthcheck_at = ? "
            "WHERE id = ?",
            (ok, total, _utcnow(), primary_id),
        )
        await self.conn.commit()

    async def get_last_healthcheck_stats(
        self, chat_id: int
    ) -> dict[int, tuple[int, int]]:
        """Return ``{primary_id: (ok, total)}`` for every primary that
        has had at least one /cekimap run. Primaries with no recorded
        run are absent from the dict (caller falls back to total alias
        count).
        """
        async with self.conn.execute(
            "SELECT id, last_healthcheck_ok, last_healthcheck_total "
            "FROM primary_accounts "
            "WHERE chat_id = ? "
            "  AND last_healthcheck_total IS NOT NULL",
            (chat_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return {
            row["id"]: (
                int(row["last_healthcheck_ok"] or 0),
                int(row["last_healthcheck_total"] or 0),
            )
            for row in rows
        }

    # ---------------------------------------------------------------- alias generator state

    async def get_generator_state(
        self, chat_id: int, primary_id: int, base: str
    ) -> GenState:
        """Return the next ``(suffix, number)`` cursor for ``base``.

        If no row exists yet, the cursor defaults to ``GenState("", 1)`` —
        i.e. the first generated name will be ``base001``.
        """
        async with self.conn.execute(
            "SELECT next_suffix, next_number FROM alias_generator_state "
            "WHERE chat_id = ? AND primary_id = ? AND base = ?",
            (chat_id, primary_id, base.strip().lower()),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return GenState("", 1)
        return GenState(row["next_suffix"] or "", int(row["next_number"]))

    async def save_generator_state(
        self,
        chat_id: int,
        primary_id: int,
        base: str,
        state: GenState,
    ) -> None:
        """Upsert the generator cursor for ``(chat_id, primary_id, base)``."""
        await self.conn.execute(
            "INSERT INTO alias_generator_state "
            "(chat_id, primary_id, base, next_suffix, next_number, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, primary_id, base) DO UPDATE SET "
            "next_suffix = excluded.next_suffix, "
            "next_number = excluded.next_number, "
            "updated_at = excluded.updated_at",
            (
                chat_id,
                primary_id,
                base.strip().lower(),
                state.suffix,
                state.number,
                _utcnow(),
            ),
        )
        await self.conn.commit()

    # ---------------------------------------------------------------- aliases

    async def add_aliases(
        self,
        chat_id: int,
        emails: Sequence[str],
        primary_id: int | None = None,
    ) -> int:
        """Insert aliases ignoring duplicates. Returns the number of new rows.

        ``primary_id`` ties each new alias to a specific primary account so
        the two-level UI can group them. ``None`` is permitted for legacy
        callers but new code should always pass a primary id.
        """
        await self.upsert_user(chat_id)
        inserted = 0
        for email in emails:
            normalized = email.strip().lower()
            if not normalized:
                continue
            cursor = await self.conn.execute(
                "INSERT OR IGNORE INTO aliases "
                "(chat_id, email, status, created_at, primary_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    chat_id,
                    normalized,
                    AliasStatus.AVAILABLE.value,
                    _utcnow(),
                    primary_id,
                ),
            )
            inserted += cursor.rowcount or 0
            # If a row already existed without a primary (migrated from the
            # legacy single-primary world), backfill its primary_id.
            if primary_id is not None and (cursor.rowcount or 0) == 0:
                await self.conn.execute(
                    "UPDATE aliases SET primary_id = ? "
                    "WHERE chat_id = ? AND email = ? AND primary_id IS NULL",
                    (primary_id, chat_id, normalized),
                )
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
        self,
        chat_id: int,
        status: AliasStatus | None = None,
        primary_id: int | None = None,
    ) -> list[AliasRecord]:
        query = (
            "SELECT id, chat_id, email, status, consumed_at, last_message_id, "
            "primary_id FROM aliases WHERE chat_id = ?"
        )
        params: list[object] = [chat_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        if primary_id is not None:
            query += " AND primary_id = ?"
            params.append(primary_id)
        query += " ORDER BY email"
        async with self.conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_alias(row) for row in rows]

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

    async def find_alias_for_primary(
        self, primary_id: int, email: str
    ) -> AliasRecord | None:
        """Look up an alias scoped to a specific primary account (used by the
        listener when deciding whether to forward an incoming email)."""
        return await self._find_alias(
            "WHERE primary_id = ? AND email = ?",
            (primary_id, email.strip().lower()),
        )

    async def _find_alias(
        self, where_clause: str, params: tuple[object, ...]
    ) -> AliasRecord | None:
        query = (
            "SELECT id, chat_id, email, status, consumed_at, last_message_id, "
            f"primary_id FROM aliases {where_clause}"
        )
        async with self.conn.execute(query, params) as cursor:
            row = await cursor.fetchone()
        return _row_to_alias(row) if row is not None else None

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

    # -------------------------------------------------------- alias_senders

    async def record_alias_sender(
        self,
        chat_id: int,
        alias_id: int,
        sender_email: str,
        sender_domain: str,
    ) -> str | None:
        """Insert/update the (alias, sender_domain) row.

        Returns the resolved service label at insert time (chat override
        first, then ``DEFAULT_SERVICE_LABELS``, walking parent domains).
        Subsequent emails from the same sender bump ``seen_count`` /
        ``last_seen_at`` only — they do *not* re-resolve the label so a
        manual rename in /services is preserved.
        """
        sender_domain = sender_domain.strip().lower()
        sender_email = sender_email.strip().lower() if sender_email else None
        if not sender_domain:
            return None
        label = await self.resolve_service_label(chat_id, sender_domain)
        now = _utcnow()
        await self.conn.execute(
            "INSERT INTO alias_senders "
            "(alias_id, chat_id, sender_email, sender_domain, "
            "service_label, first_seen_at, last_seen_at, seen_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(alias_id, sender_domain) DO UPDATE SET "
            "last_seen_at = excluded.last_seen_at, "
            "seen_count = seen_count + 1, "
            "sender_email = COALESCE(excluded.sender_email, sender_email)",
            (
                alias_id,
                chat_id,
                sender_email,
                sender_domain,
                label,
                now,
                now,
            ),
        )
        await self.conn.commit()
        return label

    async def list_alias_senders(
        self, chat_id: int, alias_id: int
    ) -> list[AliasSenderRecord]:
        """Full sender history for one alias, most-recent first."""
        async with self.conn.execute(
            "SELECT id, alias_id, chat_id, sender_email, sender_domain, "
            "service_label, first_seen_at, last_seen_at, seen_count "
            "FROM alias_senders "
            "WHERE chat_id = ? AND alias_id = ? "
            "ORDER BY last_seen_at DESC",
            (chat_id, alias_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_alias_sender(row) for row in rows]

    async def get_alias_service_labels(
        self, chat_id: int, primary_id: int | None = None
    ) -> dict[int, list[str]]:
        """Return ``{alias_id: [labels…]}`` for the chat (optionally one primary).

        Labels are re-resolved on every call so a /services edit takes
        effect immediately without backfilling old rows. Domains with
        no resolvable label appear as the raw domain so the user always
        sees *something* useful.
        """
        if primary_id is None:
            async with self.conn.execute(
                "SELECT s.alias_id, s.sender_domain, s.last_seen_at "
                "FROM alias_senders s "
                "WHERE s.chat_id = ? "
                "ORDER BY s.last_seen_at DESC",
                (chat_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self.conn.execute(
                "SELECT s.alias_id, s.sender_domain, s.last_seen_at "
                "FROM alias_senders s "
                "JOIN aliases a ON a.id = s.alias_id "
                "WHERE s.chat_id = ? AND a.primary_id = ? "
                "ORDER BY s.last_seen_at DESC",
                (chat_id, primary_id),
            ) as cursor:
                rows = await cursor.fetchall()
        # Resolve labels in bulk by domain so we don't hit the DB once
        # per row when many aliases share a sender domain. Domains the
        # user has explicitly *suppressed* (🗑 Hapus label → empty
        # chat-row) are excluded entirely so /list doesn't fall back to
        # showing the raw domain after a user already said "no thanks".
        unique_domains = {row["sender_domain"] for row in rows}
        resolved: dict[str, str | None] = {}
        for domain in unique_domains:
            label = await self.resolve_service_label(chat_id, domain)
            if label is None and await self.is_service_label_suppressed(
                chat_id, domain
            ):
                resolved[domain] = None
            else:
                resolved[domain] = label or domain
        out: dict[int, list[str]] = {}
        for row in rows:
            alias_id = row["alias_id"]
            label = resolved[row["sender_domain"]]
            if label is None:
                continue
            bucket = out.setdefault(alias_id, [])
            if label not in bucket:
                bucket.append(label)
        return out

    # -------------------------------------------------------- service_labels

    @staticmethod
    def _domain_candidates(domain: str) -> list[str]:
        """Return ``[domain, parent.tld, ..., apex.tld]`` for label lookup."""
        out: list[str] = [domain]
        parts = domain.split(".")
        while len(parts) > 2:
            parts = parts[1:]
            out.append(".".join(parts))
        return out

    async def resolve_service_label(
        self, chat_id: int, domain: str
    ) -> str | None:
        """Resolve a domain to its friendly label.

        Lookup order:
        1. Chat's own ``service_labels`` (exact match, then strip leftmost
           subdomain and retry until 2 labels remain).
           - A row with a non-empty label wins.
           - A row with an *empty* label is an explicit "suppress" sentinel:
             the resolver returns ``None`` immediately and does NOT fall
             back to ``DEFAULT_SERVICE_LABELS``.  This is what the inline
             "🗑 Hapus label" button writes when the user wants a built-in
             default to stop showing up in /list.
        2. ``DEFAULT_SERVICE_LABELS`` with the same parent-domain walk.
        3. ``None`` if nothing matches — caller usually shows the raw
           domain as a last resort.
        """
        domain = domain.strip().lower()
        if not domain:
            return None
        candidates = self._domain_candidates(domain)
        async with self.conn.execute(
            "SELECT domain, label FROM service_labels WHERE chat_id = ?",
            (chat_id,),
        ) as cursor:
            user_rows = await cursor.fetchall()
        user_map = {row["domain"]: row["label"] for row in user_rows}
        for candidate in candidates:
            if candidate in user_map:
                label = (user_map[candidate] or "").strip()
                # Empty string = explicit suppression. Stop the walk.
                return label or None
        for candidate in candidates:
            if candidate in DEFAULT_SERVICE_LABELS:
                return DEFAULT_SERVICE_LABELS[candidate]
        return None

    async def is_service_label_suppressed(
        self, chat_id: int, domain: str
    ) -> bool:
        """True if there's an explicit empty-string chat row for ``domain``
        or one of its parents — i.e., the user tapped 🗑 Hapus label.
        """
        domain = domain.strip().lower()
        if not domain:
            return False
        candidates = self._domain_candidates(domain)
        async with self.conn.execute(
            "SELECT domain FROM service_labels "
            "WHERE chat_id = ? AND (label = '' OR label IS NULL)",
            (chat_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        suppressed = {row["domain"] for row in rows}
        return any(c in suppressed for c in candidates)

    async def set_service_label(
        self, chat_id: int, domain: str, label: str
    ) -> None:
        domain = domain.strip().lower()
        label = label.strip()
        await self.upsert_user(chat_id)
        await self.conn.execute(
            "INSERT INTO service_labels (chat_id, domain, label, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, domain) DO UPDATE SET label = excluded.label",
            (chat_id, domain, label, _utcnow()),
        )
        await self.conn.commit()

    async def remove_service_label(self, chat_id: int, domain: str) -> bool:
        cursor = await self.conn.execute(
            "DELETE FROM service_labels WHERE chat_id = ? AND domain = ?",
            (chat_id, domain.strip().lower()),
        )
        await self.conn.commit()
        return (cursor.rowcount or 0) > 0

    async def list_service_labels(self, chat_id: int) -> list[tuple[str, str]]:
        async with self.conn.execute(
            "SELECT domain, label FROM service_labels "
            "WHERE chat_id = ? ORDER BY domain",
            (chat_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(row["domain"], row["label"]) for row in rows]

    # ----------------------------------------------------- forwarded_emails

    async def record_forwarded_email(
        self, chat_id: int, alias_id: int, message_id: int
    ) -> None:
        """Remember the Telegram ``message_id`` of one forwarded email so we
        can later bulk-delete it when the user switches active alias."""
        await self.conn.execute(
            "INSERT INTO forwarded_emails (chat_id, alias_id, message_id, sent_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, alias_id, message_id, _utcnow()),
        )
        await self.conn.commit()

    async def pop_forwarded_email_message_ids(
        self, chat_id: int, alias_id: int
    ) -> list[int]:
        """Return every recorded ``message_id`` for ``(chat_id, alias_id)`` and
        delete the rows in the same transaction. The bot uses the returned ids
        to call ``bot.delete_message`` so the chat doesn't accumulate stale
        forwards from a previously-active alias.
        """
        async with self.conn.execute(
            "SELECT message_id FROM forwarded_emails "
            "WHERE chat_id = ? AND alias_id = ?",
            (chat_id, alias_id),
        ) as cursor:
            rows = await cursor.fetchall()
        message_ids = [row["message_id"] for row in rows]
        if message_ids:
            await self.conn.execute(
                "DELETE FROM forwarded_emails "
                "WHERE chat_id = ? AND alias_id = ?",
                (chat_id, alias_id),
            )
            await self.conn.commit()
        return message_ids


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


def credentials_from_primary(
    primary: PrimaryAccount, plaintext_password: str
) -> BridgeCredentials:
    return BridgeCredentials(
        host=primary.imap_host,
        port=primary.imap_port,
        username=primary.imap_username,
        password=plaintext_password,
        use_ssl=primary.imap_use_ssl,
    )


def _row_to_alias(row: aiosqlite.Row) -> AliasRecord:
    return AliasRecord(
        id=row["id"],
        chat_id=row["chat_id"],
        email=row["email"],
        status=AliasStatus(row["status"]),
        consumed_at=row["consumed_at"],
        last_message_id=row["last_message_id"],
        primary_id=row["primary_id"] if "primary_id" in row.keys() else None,
    )


def _row_to_alias_sender(row: aiosqlite.Row) -> AliasSenderRecord:
    return AliasSenderRecord(
        id=row["id"],
        alias_id=row["alias_id"],
        chat_id=row["chat_id"],
        sender_email=row["sender_email"],
        sender_domain=row["sender_domain"],
        service_label=row["service_label"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        seen_count=row["seen_count"],
    )


def _row_to_primary(row: aiosqlite.Row) -> PrimaryAccount:
    return PrimaryAccount(
        id=row["id"],
        chat_id=row["chat_id"],
        email=row["email"],
        imap_host=row["imap_host"],
        imap_port=row["imap_port"],
        imap_username=row["imap_username"],
        imap_use_ssl=bool(row["imap_use_ssl"]),
        created_at=row["created_at"],
    )
