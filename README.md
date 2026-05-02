# proton-telegram-bot

Telegram bot that watches one or more Proton Mail aliases (via [Proton
Mail Bridge][bridge]) and forwards every new email to your Telegram chat.
Once an alias receives its first email it is removed from the list of
"available" aliases — handy if you mint a fresh address for every
contact and want a one-shot notification.

[bridge]: https://proton.me/mail/bridge

## Flow

1. Start the bot with `/start`.
2. Run `/connect` to enter the IMAP host / port / username / password
   that Proton Bridge exposes for your account. Credentials are
   stored encrypted at rest with a Fernet master key.
3. Run `/addalias addr1@proton.me addr2@yourdomain.com …` to register
   the aliases the bot should watch. (You can paste many at once.)
4. `/list` shows the available aliases as inline buttons. Tap one to
   confirm it — give that address to your contact.
5. As soon as an email arrives at that alias, the bot delivers the
   subject / sender / body to the chat and the alias is moved to the
   "consumed" list, disappearing from `/list`.
6. `/history` shows consumed aliases. `/reset email@…` puts an alias
   back into the available pool. `/disconnect` deletes the saved
   credentials and stops the listener.

The bot supports multiple Telegram users in parallel — each user
provides their own Proton Bridge credentials and tracks their own
aliases.

## Requirements

- Python ≥ 3.11
- A running [Proton Mail Bridge][bridge] reachable from wherever you
  run the bot (usually the same machine; Bridge listens on
  `127.0.0.1:1143` by default).
- A Telegram bot token from [@BotFather](https://t.me/botfather).

> **Multi-user heads up.** Proton Bridge only exposes IMAP on the
> machine it is running on. If you want to host one bot for several
> users, each user must either run the bot on the same machine as their
> Bridge, or expose their Bridge IMAP port to the bot host through a
> private channel (e.g. SSH tunnel, WireGuard). Don't expose Bridge
> directly to the public internet.

## Quick start (local)

```bash
git clone https://github.com/alviarts/proton-telegram-bot.git
cd proton-telegram-bot

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# fill in TELEGRAM_BOT_TOKEN and ENCRYPTION_KEY

# generate a Fernet key once:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

python -m proton_telegram_bot
```

## Deployment

### Option A — Docker (recommended)

The simplest way to deploy on a VPS that already runs Proton Bridge.

```bash
git clone https://github.com/alviarts/proton-telegram-bot.git
cd proton-telegram-bot

cp .env.example .env
# edit .env — fill in TELEGRAM_BOT_TOKEN and ENCRYPTION_KEY

docker compose up -d          # build & start
docker compose logs -f bot    # tail logs
```

`docker-compose.yml` uses `network_mode: host` so the bot can reach
Bridge on `127.0.0.1:1143` without extra configuration. Data is
persisted in a Docker volume (`bot-data`).

Useful commands:

```bash
docker compose down            # stop
docker compose up -d --build   # rebuild after a code update
docker compose logs -f bot     # watch live logs
```

### Option B — systemd service (no Docker)

1. Clone and install on your server:

```bash
sudo useradd -r -s /usr/sbin/nologin botuser
sudo mkdir -p /opt/proton-telegram-bot
sudo chown botuser:botuser /opt/proton-telegram-bot

sudo -u botuser git clone https://github.com/alviarts/proton-telegram-bot.git /opt/proton-telegram-bot
cd /opt/proton-telegram-bot
sudo -u botuser python3 -m venv .venv
sudo -u botuser .venv/bin/pip install .
```

2. Configure:

```bash
sudo -u botuser cp .env.example .env
sudo -u botuser nano .env
# fill in TELEGRAM_BOT_TOKEN and ENCRYPTION_KEY

# generate key:
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

3. Install and start the service:

```bash
sudo cp deploy/proton-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now proton-telegram-bot

# check status
sudo systemctl status proton-telegram-bot
sudo journalctl -u proton-telegram-bot -f
```

### Proton Bridge setup

The bot connects to Proton Bridge via IMAP. Make sure Bridge is running
and note the credentials it provides:

1. Install [Proton Mail Bridge](https://proton.me/mail/bridge) on your
   server or local machine.
2. Log in with your Proton account.
3. In Bridge, go to the account settings and note:
   - **IMAP host**: usually `127.0.0.1`
   - **IMAP port**: usually `1143`
   - **Username**: your Proton email address
   - **Password**: the Bridge-generated password (not your Proton password)
4. Use these credentials when running `/connect` in the Telegram bot.

> **Headless server?** Proton Bridge has a CLI mode:
> `protonmail-bridge --cli`. See the
> [Bridge documentation](https://proton.me/support/bridge) for details.

### Optional: auto-register accounts with Bridge

Set `BRIDGE_ADMIN_ENABLED=true` if the bot runs on the Bridge host and
you'd rather have `/connect` ask for your **Proton account** password
than the random Bridge IMAP password. The bot will then:

1. Stop `protonmail-bridge.service`.
2. Drive `bridge --cli login` with the email / Proton password you
   typed in `/connect`.
3. If Proton requests human verification, the bot forwards the
   verification URL to the Telegram chat — solve it in a browser, then
   reply `ok` and the bot resumes.
4. Restart `protonmail-bridge.service` and decrypt the vault to pluck
   out the per-account IMAP password Bridge just generated.
5. Save the IMAP password to the bot's DB (Fernet-encrypted) and start
   the listener — no manual copy/paste required.

Requirements (typical setup):

- The bot runs as root (or with `BRIDGE_SUDO=true` plus a sudoers rule)
  so it can `systemctl start/stop protonmail-bridge.service`.
- The `pass` keychain entry that Bridge writes on first run is
  readable by that user. The default
  `BRIDGE_VAULT_KEY_COMMAND` matches the standard install layout.
- The `cryptography`, `msgpack`, and `pexpect` packages are installed
  in the Python environment that runs the helper scripts. They are
  pulled in via the `[bridge-admin]` extra:
  `pip install -e '.[bridge-admin]'`.

When `BRIDGE_ADMIN_ENABLED=false` (default), `/connect` works the same
as before — you paste the Bridge IMAP password manually.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Token from @BotFather. |
| `ENCRYPTION_KEY` | yes | URL-safe base64 32-byte Fernet key used to encrypt stored IMAP passwords. |
| `DATABASE_PATH` | no | SQLite file path. Default: `data/bot.sqlite3`. |
| `ALLOWED_USER_IDS` | no | Comma-separated list of Telegram user IDs allowed to use the bot. Empty = anyone. |
| `LOG_LEVEL` | no | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. Default: `INFO`. |
| `ALIAS_SYNC_INTERVAL_MINUTES` | no | How often (in minutes) to re-scan the inbox for new aliases. Default: `5`. |
| `BRIDGE_ADMIN_ENABLED` | no | Set to `true` to let `/connect` accept your **Proton account** password and auto-register the account with Proton Bridge (instead of asking for the Bridge IMAP password). Requires the bot to run on the Bridge host with permission to call `systemctl` and read the Bridge vault key. Default: `false`. |
| `BRIDGE_ADD_ACCOUNT_SCRIPT` | no | Path to `bridge_add_account.py`. Default: `scripts/bridge_add_account.py`. |
| `BRIDGE_DECRYPT_VAULT_SCRIPT` | no | Path to `bridge_decrypt_vault.py`. Default: `scripts/bridge_decrypt_vault.py`. |
| `BRIDGE_VAULT_PATH` | no | Bridge encrypted-vault file. Default: `/root/.config/protonmail/bridge-v3/vault.enc`. |
| `BRIDGE_VAULT_KEY_COMMAND` | no | Shell command (run with `sh -c`) that prints the raw vault key on stdout. Default: `pass show docker-credential-helpers/<base64>/bridge-vault-key`. |
| `BRIDGE_CAPTCHA_URL_FILE` | no | Path the helper uses to write a CAPTCHA verification URL. Default: `/tmp/bridge_captcha_url.txt`. |
| `BRIDGE_CAPTCHA_DONE_FLAG` | no | Path the bot creates once the user has solved the CAPTCHA. Default: `/tmp/bridge_captcha_done.flag`. |
| `BRIDGE_CAPTCHA_TIMEOUT_SECONDS` | no | How long the helper waits for the user to solve a CAPTCHA. Default: `600`. |
| `BRIDGE_PYTHON` | no | Python interpreter used to run the helper scripts. Default: `python3`. |
| `BRIDGE_SUDO` | no | Set to `true` to wrap helper invocations in `sudo -n …`. Default: `false`. |

## Telegram commands

| Command | Description |
|---|---|
| `/start` | Register your chat and show available aliases. |
| `/connect` | Guided dialog to store your Proton Bridge IMAP credentials. |
| `/disconnect` | Delete stored credentials and stop watching your inbox. |
| `/addalias a@b.com c@d.com …` | Register one or more aliases. Repeats are ignored. |
| `/sync user password` | Auto-sync all addresses from your Proton account. |
| `/removealias a@b.com` | Forget an alias entirely. |
| `/list` | Show available aliases as inline buttons. |
| `/accounts` | Shortcut for `/list`. |
| `/history` | Show aliases that already received their email. |
| `/reset a@b.com` | Move an alias back to "available". |
| `/setprotonpw` | Store your Proton master password (used by `/genaddr`). |
| `/genaddr <base> <count> [@domain]` | Auto-create N addresses on Proton via headless browser. |
| `/cancel` | Abort the current `/connect` / `/setprotonpw` dialog. |

### Bulk address creation (`/genaddr`)

Drives the official Proton account UI through a headless Chromium so
the address-key cryptography is performed by Proton's own JavaScript —
the bot never touches your private keys. Names are generated
deterministically:

```
/genaddr vielz 10
# → vielz001 … vielz010 @ proton.me

/genaddr vielz 50 @proton.me
# resumes from the last successful number; on overflow appends a
#   letter suffix (vielz999 → vielza001 → … → vielzz999 → vielzaa001).
```

Prerequisites:

1. `/connect` an account first (Bridge credentials).
2. `/setprotonpw` to store the Proton **master** password (Fernet-encrypted
   alongside Bridge credentials). This is required because
   address-key generation in the web UI needs it.
3. Make sure your Proton plan permits enough addresses (Business / Mail
   Plus / Unlimited). Already-existing names are detected and skipped.

If Proton presents a CAPTCHA mid-batch the bot stops and tells you which
name it stopped on; solve in the browser, then re-run `/genaddr` with
the same arguments — the cursor resumes where it left off.

## Development

```bash
pip install -e ".[dev]"
ruff check .
pytest
```

The bot is intentionally small and uses only the standard library +
`python-telegram-bot`, `aioimaplib`, `aiosqlite`, `cryptography`,
`pydantic-settings`. Persistence is a single SQLite file; per-user
state is fully isolated.

## Security notes

- Bridge passwords are encrypted at rest with `cryptography.Fernet`.
  Lose the `ENCRYPTION_KEY` and you will need to `/connect` again.
- The Proton **master password** stored by `/setprotonpw` is encrypted
  the same way, but unlike a Bridge password it cannot be revoked
  per-device — anyone with both your `.env` (containing the
  `ENCRYPTION_KEY`) **and** the SQLite DB can sign in to your Proton
  account fully. Only enable `/setprotonpw` on a host you treat as
  authoritative, and rotate the Proton password if the host is ever
  compromised.
- Telegram message bodies may contain sensitive content. Treat the
  chat history as you would your inbox.
- Never commit your `.env` or the SQLite database. Both are excluded
  by `.gitignore`.
- Set `ALLOWED_USER_IDS` to restrict access to your Telegram user ID
  only. Without it anyone who finds your bot can use it.

## License

MIT — see [LICENSE](LICENSE).
