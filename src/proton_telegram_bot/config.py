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
    captcha_helper_base_url: str = Field(default="")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allowed_user_ids(self) -> list[int]:
        raw = self.allowed_user_ids_raw or ""
        return [int(part.strip()) for part in raw.split(",") if part.strip()]


def load_settings() -> Settings:
    """Load and validate settings from the environment."""
    return Settings()  # type: ignore[call-arg]
