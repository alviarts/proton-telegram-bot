"""Domain models for users and aliases."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AliasStatus(StrEnum):
    AVAILABLE = "available"
    CONSUMED = "consumed"


@dataclass(slots=True)
class BridgeCredentials:
    """IMAP credentials for a user's Proton Bridge instance."""

    host: str
    port: int
    username: str
    password: str
    use_ssl: bool = False  # Bridge defaults to STARTTLS on port 1143

    def display(self) -> str:
        return f"{self.username}@{self.host}:{self.port} (ssl={self.use_ssl})"


@dataclass(slots=True)
class UserRecord:
    chat_id: int
    has_credentials: bool
    imap_host: str | None = None
    imap_port: int | None = None
    imap_username: str | None = None
    imap_use_ssl: bool = False


@dataclass(slots=True)
class AliasRecord:
    id: int
    chat_id: int
    email: str
    status: AliasStatus
    consumed_at: str | None = None
    last_message_id: str | None = None
