from __future__ import annotations

import os
from pathlib import Path

from proton_telegram_bot.config import load_settings


def test_load_settings_parses_user_ids(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("ENCRYPTION_KEY", "dummy-key")
    monkeypatch.setenv("ALLOWED_USER_IDS", " 12 , 34, 56 ")
    monkeypatch.setenv("DATABASE_PATH", "data/x.sqlite3")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    settings = load_settings()
    assert settings.telegram_bot_token == "test-token"
    assert settings.encryption_key == "dummy-key"
    assert settings.allowed_user_ids == [12, 34, 56]
    assert settings.database_path == Path("data/x.sqlite3")
    assert settings.log_level == "DEBUG"


def test_load_settings_empty_user_ids(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("ENCRYPTION_KEY", "k")
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)
    settings = load_settings()
    assert settings.allowed_user_ids == []
    # Make sure nothing leaks across tests
    os.environ.pop("ALLOWED_USER_IDS", None)
