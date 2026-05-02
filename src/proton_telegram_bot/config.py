"""Application configuration loaded from environment variables."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the bot."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    telegram_bot_token: str = Field(..., min_length=1)
    encryption_key: str = Field(..., min_length=1)
    database_path: Path = Field(default=Path("data/bot.sqlite3"))
    # Stored as a raw string; parsed by allowed_user_ids below. We avoid a list[int]
    # field because pydantic-settings tries to JSON-decode list env vars before
    # validators run, which makes a comma-separated value awkward.
    allowed_user_ids_raw: str = Field(default="", alias="ALLOWED_USER_IDS")
    log_level: str = Field(default="INFO")
    alias_sync_interval_minutes: int = Field(default=5)

    # --- Optional Bridge auto-add support ---
    # When enabled, /connect can use the user's *Proton account* password to
    # automatically add the account to the host's Proton Bridge (via
    # ``bridge --cli``), then extract the per-account IMAP password from the
    # encrypted vault. This requires the bot to run on the same host as
    # Bridge with permission to call ``systemctl`` and read the vault key.
    bridge_admin_enabled: bool = Field(default=False, alias="BRIDGE_ADMIN_ENABLED")
    bridge_add_account_script: Path = Field(
        default=Path("scripts/bridge_add_account.py"),
        alias="BRIDGE_ADD_ACCOUNT_SCRIPT",
    )
    bridge_remove_account_script: Path = Field(
        default=Path("scripts/bridge_remove_account.py"),
        alias="BRIDGE_REMOVE_ACCOUNT_SCRIPT",
    )
    bridge_decrypt_vault_script: Path = Field(
        default=Path("scripts/bridge_decrypt_vault.py"),
        alias="BRIDGE_DECRYPT_VAULT_SCRIPT",
    )
    bridge_vault_remove_user_script: Path = Field(
        default=Path("scripts/bridge_vault_remove_user.py"),
        alias="BRIDGE_VAULT_REMOVE_USER_SCRIPT",
    )
    bridge_vault_path: Path = Field(
        default=Path("/root/.config/protonmail/bridge-v3/vault.enc"),
        alias="BRIDGE_VAULT_PATH",
    )
    # Shell command (single string passed to ``sh -c``) that prints the
    # raw vault key to stdout. Default uses the standard ``pass`` keychain
    # entry that Proton Bridge writes on first run.
    bridge_vault_key_command: str = Field(
        default=(
            "pass show docker-credential-helpers/"
            "cHJvdG9ubWFpbC9icmlkZ2UtdjMvdXNlcnMvYnJpZGdlLXZhdWx0LWtleQ=="
            "/bridge-vault-key"
        ),
        alias="BRIDGE_VAULT_KEY_COMMAND",
    )
    bridge_captcha_url_file: Path = Field(
        default=Path("/tmp/bridge_captcha_url.txt"),
        alias="BRIDGE_CAPTCHA_URL_FILE",
    )
    bridge_captcha_done_flag: Path = Field(
        default=Path("/tmp/bridge_captcha_done.flag"),
        alias="BRIDGE_CAPTCHA_DONE_FLAG",
    )
    bridge_captcha_timeout_seconds: int = Field(
        default=600, alias="BRIDGE_CAPTCHA_TIMEOUT_SECONDS"
    )
    bridge_python: str = Field(default="python3", alias="BRIDGE_PYTHON")
    bridge_sudo: bool = Field(default=False, alias="BRIDGE_SUDO")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allowed_user_ids(self) -> list[int]:
        raw = self.allowed_user_ids_raw or ""
        return [int(part.strip()) for part in raw.split(",") if part.strip()]


def load_settings() -> Settings:
    """Load and validate settings from the environment."""
    return Settings()  # type: ignore[call-arg]
