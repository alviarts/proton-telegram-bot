"""Tests for the BridgeAdmin module.

These tests stub out the helper subprocesses with a tiny fake, so they
run anywhere — including in CI without a real Proton Bridge install.
"""
from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest

from proton_telegram_bot.bridge_admin import (
    BridgeAdmin,
    BridgeAdminError,
    CaptchaRequired,
    LoginFailed,
    LoginSucceeded,
)
from proton_telegram_bot.config import Settings


def _settings(
    tmp_path: Path,
    *,
    add_script: str,
    decrypt_script: str | None = None,
    vault_remove_script: str | None = None,
    enabled: bool = True,
    captcha_timeout: int = 5,
) -> Settings:
    add_path = tmp_path / "add_account.py"
    add_path.write_text(add_script)
    add_path.chmod(0o755)

    decrypt_path = tmp_path / "decrypt_vault.py"
    decrypt_path.write_text(decrypt_script or "")

    vault_remove_path = tmp_path / "vault_remove_user.py"
    vault_remove_path.write_text(
        vault_remove_script
        or "#!/usr/bin/env python3\nimport sys; sys.stdin.read(); sys.exit(0)\n"
    )

    captcha_url_file = tmp_path / "captcha_url.txt"
    captcha_done_flag = tmp_path / "captcha_done.flag"
    vault_path = tmp_path / "vault.enc"
    vault_path.write_bytes(b"not-actually-decrypted")

    return Settings(
        telegram_bot_token="t",
        encryption_key="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        database_path=tmp_path / "db.sqlite3",
        bridge_admin_enabled=enabled,
        bridge_add_account_script=add_path,
        bridge_decrypt_vault_script=decrypt_path,
        bridge_vault_remove_user_script=vault_remove_path,
        bridge_vault_path=vault_path,
        # Echo a fixed string so tests don't need a real `pass` install.
        bridge_vault_key_command="printf '%s' 'YWFhYWFhYWFhYWFhYWFhYQ=='",
        bridge_captcha_url_file=captcha_url_file,
        bridge_captcha_done_flag=captcha_done_flag,
        bridge_captcha_timeout_seconds=captcha_timeout,
        bridge_python=sys.executable,
        bridge_sudo=False,
    )


@pytest.fixture
def fake_systemctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Inject a no-op ``systemctl`` into PATH so remove_account works in CI."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "systemctl"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    existing_path = "/usr/bin:/bin"
    monkeypatch.setenv("PATH", f"{bin_dir}:{existing_path}")
    return fake


@pytest.mark.asyncio
async def test_disabled_by_default(tmp_path: Path) -> None:
    s = Settings(
        telegram_bot_token="t",
        encryption_key="x" * 32,
        database_path=tmp_path / "db.sqlite3",
    )
    admin = BridgeAdmin(s)
    assert admin.enabled is False
    with pytest.raises(BridgeAdminError):
        async for _ in admin.add_account("a@b.com", "p"):
            pass


@pytest.mark.asyncio
async def test_add_account_success(tmp_path: Path) -> None:
    """Helper that exits 0 immediately yields a single LoginSucceeded."""
    s = _settings(
        tmp_path,
        add_script=textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys
            sys.exit(0)
            """
        ),
    )
    admin = BridgeAdmin(s)
    events = [event async for event in admin.add_account("a@b.com", "p")]
    assert len(events) == 1
    assert isinstance(events[0], LoginSucceeded)


@pytest.mark.asyncio
async def test_add_account_failed_propagates_stderr(tmp_path: Path) -> None:
    s = _settings(
        tmp_path,
        add_script=textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys
            sys.stderr.write("incorrect-credentials\\n")
            sys.exit(1)
            """
        ),
    )
    admin = BridgeAdmin(s)
    events = [event async for event in admin.add_account("a@b.com", "p")]
    assert len(events) == 1
    assert isinstance(events[0], LoginFailed)
    assert "incorrect-credentials" in events[0].reason


@pytest.mark.asyncio
async def test_add_account_captcha_then_succeed(tmp_path: Path) -> None:
    """Helper writes URL → waits for ack flag → exits 0 → ``LoginSucceeded``."""
    captcha_url = tmp_path / "captcha_url.txt"
    flag = tmp_path / "captcha_done.flag"
    s = _settings(
        tmp_path,
        captcha_timeout=20,
        add_script=textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import os, sys, time
            from pathlib import Path
            Path({str(captcha_url)!r}).write_text("https://verify.proton.me/?token=abc")
            for _ in range(60):
                if Path({str(flag)!r}).exists():
                    Path({str(flag)!r}).unlink()
                    break
                time.sleep(0.1)
            else:
                sys.exit(2)
            sys.exit(0)
            """
        ),
    )
    admin = BridgeAdmin(s)

    async def driver() -> list:
        events = []
        async for event in admin.add_account("a@b.com", "p"):
            events.append(event)
            if isinstance(event, CaptchaRequired):
                await asyncio.sleep(0.05)
                await admin.acknowledge_captcha()
        return events

    events = await asyncio.wait_for(driver(), timeout=10)
    assert any(isinstance(e, CaptchaRequired) for e in events)
    assert any(isinstance(e, LoginSucceeded) for e in events)


@pytest.mark.asyncio
async def test_fetch_imap_credentials_finds_match(tmp_path: Path) -> None:
    decrypt_script = textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import json, sys
        # Drain the key from stdin so the harness exits cleanly.
        sys.stdin.read()
        json.dump({"Users": [
            {
                "PrimaryEmail": "vielz99@proton.me",
                "ImapUsername": "vielz99@proton.me",
                "ImapPassword": "abc-DEF_123",
            },
            {
                "PrimaryEmail": "other@proton.me",
                "ImapUsername": "other@proton.me",
                "ImapPassword": "z",
            },
        ]}, sys.stdout)
        """
    )
    s = _settings(
        tmp_path,
        add_script="#!/usr/bin/env python3\nimport sys; sys.exit(0)\n",
        decrypt_script=decrypt_script,
    )
    admin = BridgeAdmin(s)
    creds = await admin.fetch_imap_credentials("vielz99@proton.me")
    assert creds is not None
    assert creds.email == "vielz99@proton.me"
    assert creds.imap_username == "vielz99@proton.me"
    assert creds.imap_password == "abc-DEF_123"


@pytest.mark.asyncio
async def test_fetch_imap_credentials_returns_none_for_missing(tmp_path: Path) -> None:
    decrypt_script = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.read()\n"
        + 'json.dump({"Users": []}, sys.stdout)\n'
    )
    s = _settings(
        tmp_path,
        add_script="#!/usr/bin/env python3\nimport sys; sys.exit(0)\n",
        decrypt_script=decrypt_script,
    )
    admin = BridgeAdmin(s)
    creds = await admin.fetch_imap_credentials("nope@proton.me")
    assert creds is None


@pytest.mark.asyncio
async def test_fetch_imap_credentials_raises_on_decrypt_failure(tmp_path: Path) -> None:
    decrypt_script = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stderr.write('boom\\n')\n"
        "sys.exit(2)\n"
    )
    s = _settings(
        tmp_path,
        add_script="#!/usr/bin/env python3\nimport sys; sys.exit(0)\n",
        decrypt_script=decrypt_script,
    )
    admin = BridgeAdmin(s)
    with pytest.raises(BridgeAdminError):
        await admin.fetch_imap_credentials("a@b.com")


@pytest.mark.asyncio
async def test_remove_account_runs_vault_rewrite_with_key_on_stdin(
    tmp_path: Path, fake_systemctl: Path
) -> None:
    """``remove_account`` should pipe the vault key to the rewrite script.

    The rewrite script writes the (target, raw key, vault path) triple it
    received into a sentinel file so we can assert all three were forwarded.
    """
    log = tmp_path / "remove_called.txt"
    vault_remove_script = textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import sys
        target = sys.argv[1]
        vault = sys.argv[2]
        key = sys.stdin.read().strip()
        with open({str(log)!r}, "w") as fh:
            fh.write(f"{{target}}|{{vault}}|{{key}}")
        sys.exit(0)
        """
    )
    s = _settings(
        tmp_path,
        add_script="#!/usr/bin/env python3\nimport sys; sys.exit(0)\n",
        vault_remove_script=vault_remove_script,
    )
    admin = BridgeAdmin(s)
    ok = await admin.remove_account("vielz99@proton.me")
    assert ok is True
    target, vault, key = log.read_text().split("|")
    assert target == "vielz99@proton.me"
    assert vault == str(s.bridge_vault_path)
    assert key == "YWFhYWFhYWFhYWFhYWFhYQ=="


@pytest.mark.asyncio
async def test_remove_account_returns_false_on_script_failure(
    tmp_path: Path, fake_systemctl: Path
) -> None:
    """A non-zero exit from the vault rewrite must not crash the caller."""
    s = _settings(
        tmp_path,
        add_script="#!/usr/bin/env python3\nimport sys; sys.exit(0)\n",
        vault_remove_script=(
            "#!/usr/bin/env python3\nimport sys\n"
            "sys.stdin.read()\nsys.stderr.write('vault rewrite failed\\n')\n"
            "sys.exit(1)\n"
        ),
    )
    admin = BridgeAdmin(s)
    ok = await admin.remove_account("vielz99@proton.me")
    assert ok is False


@pytest.mark.asyncio
async def test_remove_account_disabled_raises(tmp_path: Path) -> None:
    s = _settings(
        tmp_path,
        add_script="#!/usr/bin/env python3\nimport sys; sys.exit(0)\n",
        enabled=False,
    )
    admin = BridgeAdmin(s)
    with pytest.raises(BridgeAdminError):
        await admin.remove_account("a@b.com")
